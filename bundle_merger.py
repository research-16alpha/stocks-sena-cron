"""
bundle_merger.py
================
Combines THREE data sources per symbol into one final fundamentals bundle:

  1. EXISTING bundle      (F:\\expansion\\stocks-sena\\backup\\fundamentals\\{sym}.json)
     = current production. Already contains BSE legacy CSV (FY07-13) +
       NSE/BSE XBRL (FY18-FY25) merged from prior ingests.

  2. MEDALLION bundle     (F:\\expansion\\stocks-sena\\medallion\\{sym}.json)
     = Screener.in scrape. 12 yrs (FY14-FY25) full BS+CF+PL+ratios+shareholding.
       Fills the FY14-FY23 CF/full-BS gap that XBRL doesn't cover.

  3. (future) AR PDF      = pre-FY14 CF + full BS. Out of scope this sprint.

Merge rules (per period, per field):
  - XBRL (from existing) wins when present  — MCA verified primary source.
  - Medallion fills NULLs and adds missing periods.
  - BSE legacy CSV (from existing, FY07-13) stays untouched (older than medallion).
  - Snapshot: keep existing if any field non-null, else use medallion.
  - v2 fields (*_consolidated, *_standalone) kept from existing as-is.
  - Every row carries _source: 'xbrl_mca' | 'screener_medallion' | 'bse_legacy'.

Outputs to F:\\expansion\\stocks-sena\\merged\\{sym}.json (NEVER overwrites
existing inputs).

Usage:
  python bundle_merger.py                     # all symbols where any input exists
  python bundle_merger.py --syms RELIANCE,TCS # specific symbols
  python bundle_merger.py --rebuild           # rebuild all
  python bundle_merger.py --limit 10
"""
import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Set

EXISTING_DIR  = r'F:\expansion\stocks-sena\bse_v4'         # PRIMARY: extended-parser BSE output (Sprint 2)
BSE_V2_DIR    = r'F:\expansion\stocks-sena\nse_v1'         # SECONDARY: NSE XBRL re-parsed (covers stocks BSE missed)
MEDALLION_DIR = r'F:\expansion\stocks-sena\medallion'      # TERTIARY: Screener fallback (only for years no XBRL exists)
MERGED_DIR    = r'F:\expansion\stocks-sena\merged_v3'      # fresh output for Sprint 2 enriched bundles

# Source-tag priorities (higher number wins when both have a value)
PRIORITY = {
    'xbrl_mca': 3,           # MCA-filed XBRL (existing bundle pl_consolidated etc)
    'bse_legacy': 2,         # BSE legacy CSV (FY07-13)
    'screener_medallion': 1, # Screener scrape
    None: 0,
}


def load_json(path: str) -> Optional[Dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'  [WARN] load {path}: {e}', file=sys.stderr)
        return None


def atomic_write(path: str, data: Dict) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, default=str, separators=(',', ':'))
    os.replace(tmp, path)


def infer_source_for_existing_row(row: Dict, default: str = 'xbrl_mca') -> str:
    """Existing bundles from bse_ingest didn't carry _source tags. Infer by content:
    - Rich field set (cost_of_materials, finance_costs etc.) = XBRL
    - Minimal set (just sales/expenses/net_profit/eps) = legacy CSV
    """
    if row.get('_source'):
        return row['_source']
    rich_markers = {'cost_of_materials', 'employee_benefit_expense', 'finance_costs',
                    'other_expenses', 'current_assets', 'property_plant_equipment',
                    'wc_change_inventory', 'cfo_before_wc', 'capex'}
    has_rich = any(k in row and row[k] is not None for k in rich_markers)
    if has_rich:
        return 'xbrl_mca'
    # Distinguish further by period — FY07-13 → legacy
    period = row.get('period', '')
    try:
        yr = int(period[:4])
        if yr <= 2013:
            return 'bse_legacy'
    except Exception:
        pass
    return default


def merge_period_rows(existing: List[Dict], medallion: List[Dict],
                      default_existing_source: str = 'xbrl_mca',
                      default_medallion_source: str = 'screener_medallion') -> List[Dict]:
    """
    Merge two lists of period-keyed rows. Per-period, per-field:
      - Higher-priority source wins where both have a value.
      - Lower-priority source fills NULLs.
      - Periods present in only one source pass through with that source's tag.

    `default_medallion_source` should be set to 'xbrl_mca' when the second
    argument is also XBRL data (e.g. bse_v3 bundles) rather than Screener.
    Failing to do so causes XBRL data to be mislabeled as `screener_medallion`.
    """
    by_period: Dict[str, Dict] = {}

    # Index existing
    for row in (existing or []):
        period = row.get('period')
        if not period:
            continue
        src = infer_source_for_existing_row(row, default=default_existing_source)
        # Strip _source from individual fields, keep at row level
        cleaned = {k: v for k, v in row.items() if k != '_source'}
        by_period[period] = {'_row': cleaned, '_source': src}

    # Merge in medallion
    for row in (medallion or []):
        period = row.get('period')
        if not period:
            continue
        med_src = row.get('_source', default_medallion_source)
        med_cleaned = {k: v for k, v in row.items() if k != '_source'}

        if period not in by_period:
            by_period[period] = {'_row': med_cleaned, '_source': med_src}
            continue

        existing_entry = by_period[period]
        existing_row = existing_entry['_row']
        existing_src = existing_entry['_source']
        existing_prio = PRIORITY.get(existing_src, 0)
        med_prio = PRIORITY.get(med_src, 0)

        merged = dict(existing_row)
        # Track per-field sources for transparency
        field_sources: Dict[str, str] = {}
        for k, v in existing_row.items():
            if k == 'period':
                continue
            if v is not None:
                field_sources[k] = existing_src

        for k, v in med_cleaned.items():
            if k == 'period':
                continue
            if v is None:
                continue
            cur = merged.get(k)
            if cur is None:
                # Existing didn't have it
                merged[k] = v
                field_sources[k] = med_src
            else:
                # Both have it — higher priority wins
                if med_prio > existing_prio:
                    merged[k] = v
                    field_sources[k] = med_src
                # else keep existing

        # Choose row-level _source: highest priority among non-null fields
        srcs = set(field_sources.values()) | {existing_src}
        chosen = max(srcs, key=lambda s: PRIORITY.get(s, 0))
        by_period[period] = {'_row': merged, '_source': chosen,
                             '_field_sources': field_sources}

    # Build output sorted by period asc
    out: List[Dict] = []
    for period in sorted(by_period.keys()):
        entry = by_period[period]
        row = dict(entry['_row'])
        row['_source'] = entry['_source']
        # _field_sources is informative but bloats the JSON — keep only when
        # mixed (more than one source contributed)
        fs = entry.get('_field_sources') or {}
        srcs = set(fs.values())
        if len(srcs) > 1:
            row['_field_sources'] = fs
        out.append(row)
    return out


def merge_snapshot(existing: List[Dict], medallion: List[Dict]) -> List[Dict]:
    """Snapshot is a tiny list — usually 1 row. Keep existing if any field non-null,
    else use medallion."""
    def is_useful(rows):
        if not rows:
            return False
        for r in rows:
            for k, v in r.items():
                if k in ('symbol', 'fetched_date', '_source'):
                    continue
                if v is not None:
                    return True
        return False
    if is_useful(existing):
        # Annotate _source on each row if missing
        out = []
        for r in existing:
            rr = dict(r)
            rr.setdefault('_source', 'xbrl_mca')
            out.append(rr)
        return out
    if is_useful(medallion):
        return medallion
    return existing or medallion or []


def merge_bundle(sym: str) -> Optional[Dict]:
    existing  = load_json(os.path.join(EXISTING_DIR,  f'{sym}.json'))
    bse_v2    = load_json(os.path.join(BSE_V2_DIR,    f'{sym}.json'))
    medallion = load_json(os.path.join(MEDALLION_DIR, f'{sym}.json'))

    if not existing and not bse_v2 and not medallion:
        return None

    existing  = existing  or {}
    bse_v2    = bse_v2    or {}
    medallion = medallion or {}

    def merge3(a_existing: List, b_v2: List, c_medallion: List,
               existing_default_src: str = 'xbrl_mca',
               v2_default_src: str = 'xbrl_mca') -> List[Dict]:
        """Merge in priority order: existing -> bse_v2 -> medallion.
        Higher priority earlier in chain wins on conflicts; later fills nulls."""
        # First merge existing + bse_v2 (both XBRL-class sources — both labeled xbrl_mca)
        step1 = merge_period_rows(
            a_existing, b_v2,
            default_existing_source=existing_default_src,
            default_medallion_source=v2_default_src,
        )
        # Then merge with medallion (lower priority — labeled screener_medallion)
        return merge_period_rows(
            step1, c_medallion,
            default_existing_source=existing_default_src,
            default_medallion_source='screener_medallion',
        )

    merged: Dict = {
        'symbol': sym,
        'snapshot': merge_snapshot(
            existing.get('snapshot') or bse_v2.get('snapshot') or [],
            medallion.get('snapshot') or [],
        ),
        'annual_pl':         merge3(existing.get('annual_pl') or [],
                                     bse_v2.get('annual_pl') or [],
                                     medallion.get('annual_pl') or []),
        'annual_bs':         merge3(existing.get('annual_bs') or [],
                                     bse_v2.get('annual_bs') or [],
                                     medallion.get('annual_bs') or []),
        'annual_cf':         merge3(existing.get('annual_cf') or [],
                                     bse_v2.get('annual_cf') or [],
                                     medallion.get('annual_cf') or []),
        'annual_ratios':     merge3(existing.get('annual_ratios') or [],
                                     bse_v2.get('annual_ratios') or [],
                                     medallion.get('annual_ratios') or []),
        'quarterly_results': merge3(existing.get('quarterly_results') or [],
                                     bse_v2.get('quarterly_results') or [],
                                     medallion.get('quarterly_results') or []),
        'shareholding':      merge3(existing.get('shareholding') or [],
                                     bse_v2.get('shareholding') or [],
                                     medallion.get('shareholding') or []),
    }

    # Carry v2 split-by-basis fields from existing or bse_v2 (XBRL sources have them)
    # Both existing and v2 are XBRL — label both as xbrl_mca, NOT screener_medallion.
    for key in ('annual_pl_consolidated', 'annual_pl_standalone',
                'annual_bs_consolidated', 'annual_bs_standalone',
                'annual_cf_consolidated', 'annual_cf_standalone',
                'quarterly_results_consolidated', 'quarterly_results_standalone',
                'annual_ratios_filed', 'segments_annual', 'segments_quarterly'):
        existing_val = existing.get(key)
        v2_val = bse_v2.get(key)
        if existing_val and v2_val and isinstance(existing_val, list) and isinstance(v2_val, list):
            merged[key] = merge_period_rows(
                existing_val, v2_val,
                default_existing_source='xbrl_mca',
                default_medallion_source='xbrl_mca',
            )
        elif existing_val:
            merged[key] = existing_val
        elif v2_val:
            merged[key] = v2_val

    # Provenance summary
    existing_prov  = existing.get('provenance')  or {}
    v2_prov        = bse_v2.get('provenance')    or {}
    medallion_prov = medallion.get('provenance') or {}
    merged['provenance'] = {
        'source': 'merged',
        'merged_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'inputs': {
            'existing': {
                'present': bool(existing),
                'source': existing_prov.get('source'),
                'parsed_at': existing_prov.get('parsed_at'),
                'scrip_code': existing_prov.get('scrip_code'),
            },
            'bse_v2': {
                'present': bool(bse_v2),
                'source': v2_prov.get('source'),
                'parsed_at': v2_prov.get('parsed_at'),
                'scrip_code': v2_prov.get('scrip_code'),
            },
            'medallion': {
                'present': bool(medallion),
                'source': medallion_prov.get('source'),
                'ingested_at': medallion_prov.get('ingested_at'),
            },
        },
        'stats': {
            'annual_pl_years': len(merged['annual_pl']),
            'annual_bs_years': len(merged['annual_bs']),
            'annual_cf_years': len(merged['annual_cf']),
            'quarterly_periods': len(merged['quarterly_results']),
        },
    }
    return merged


def list_all_symbols() -> List[str]:
    """Union of symbols present in any source."""
    syms: Set[str] = set()
    for d in (EXISTING_DIR, BSE_V2_DIR, MEDALLION_DIR):
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith('.json') and not f.startswith('_'):
                    syms.add(f[:-5].upper())
    return sorted(syms)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--syms', type=str, default='')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    os.makedirs(MERGED_DIR, exist_ok=True)

    if args.syms:
        syms = [s.strip().upper() for s in args.syms.split(',') if s.strip()]
    else:
        syms = list_all_symbols()
    if args.limit:
        syms = syms[:args.limit]

    print(f'[INFO] Existing dir : {EXISTING_DIR}')
    print(f'[INFO] Medallion dir: {MEDALLION_DIR}')
    print(f'[INFO] Output dir   : {MERGED_DIR}')
    print(f'[INFO] Symbols      : {len(syms)}')

    ok = 0; empty = 0; skipped = 0; errors = []
    t0 = time.time()

    for i, sym in enumerate(syms, 1):
        out_path = os.path.join(MERGED_DIR, f'{sym}.json')
        if not args.rebuild and os.path.exists(out_path):
            skipped += 1
            continue
        try:
            b = merge_bundle(sym)
            if b is None:
                empty += 1
                continue
            atomic_write(out_path, b)
            ok += 1
            if i <= 10 or i % 200 == 0:
                p = b['provenance']['stats']
                inputs = b['provenance']['inputs']
                tag  = 'E' if inputs['existing']['present'] else '-'
                tag += 'B' if inputs['bse_v2']['present'] else '-'
                tag += 'M' if inputs['medallion']['present'] else '-'
                print(f'[{i}/{len(syms)}] {sym:<14} OK  [{tag}]  '
                      f'pl={p["annual_pl_years"]} bs={p["annual_bs_years"]} '
                      f'cf={p["annual_cf_years"]} q={p["quarterly_periods"]}')
        except Exception as e:
            errors.append({'symbol': sym, 'error': str(e)})
            print(f'[{i}/{len(syms)}] {sym:<14} ERR  {e}', file=sys.stderr)

    print()
    print('-' * 60)
    print(f'  Total            : {len(syms)}')
    print(f'  Merged           : {ok}')
    print(f'  Skipped (resume) : {skipped}')
    print(f'  Empty            : {empty}')
    print(f'  Errors           : {len(errors)}')
    print(f'  Elapsed          : {time.time()-t0:.1f}s')
    if errors[:5]:
        print('  First errors:')
        for e in errors[:5]:
            print(f'    {e["symbol"]:<14} {e["error"]}')


if __name__ == '__main__':
    main()
