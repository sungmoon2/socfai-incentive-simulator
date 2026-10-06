#!/usr/bin/env python3
"""Run a frozen model and verify its scientific result against a fixed reference."""
import argparse
import hashlib
import importlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

VERSION = '1.0.0'
ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PINS = {'source-manifest.json': 'e1275d22bc0a652fb7b331124952b685fef7bb31aa0cf458702438c23534ab67', 'reference-manifest.json': '7920b80bd768e3f8ffab45c5fbaa45d7089941ed43980d5b892af3865cb29567'}
CONFIGS = {('dispatch', 'smoke'): 'smoke.json', ('dispatch', 'formal'): 'formal.json',
           ('cy', 'smoke'): 'cy_v2_smoke.json', ('cy', 'formal'): 'cy_v2_formal.json'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def checked_file(relative):
    path = ROOT / relative
    if path.resolve() != path or not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError('nonlocal or symlink input: ' + relative)
    return path.read_bytes()


def verify_inputs():
    if sys.version_info[:2] != (3, 12):
        raise ValueError('Python 3.12 required')
    manifests = {}
    if set(MANIFEST_PINS) != {'source-manifest.json', 'reference-manifest.json'}:
        raise ValueError('reference pins are incomplete')
    for name, expected in MANIFEST_PINS.items():
        raw = checked_file('results/' + name)
        if sha(raw) != expected:
            raise ValueError('manifest hash mismatch: ' + name)
        manifests[name] = json.loads(raw)
    source = manifests['source-manifest.json']
    references = manifests['reference-manifest.json']['references']
    for entry in source['files'] + [f for r in references for f in r['files']]:
        raw = checked_file(entry['path'])
        if len(raw) != entry['bytes'] or sha(raw) != entry['sha256']:
            raise ValueError('file integrity mismatch: ' + entry['path'])
    expected = {f['path'] for f in source['files'] if f['path'].startswith(('src/', 'configs/'))}
    actual = {f.relative_to(ROOT).as_posix() for folder in ('src', 'configs')
              for f in (ROOT / folder).rglob('*') if f.is_file() or f.is_symlink()}
    if actual != expected:
        raise ValueError('source/config inventory mismatch; remove unlisted files or bytecode caches')
    return source, references


def check_imports(source):
    approved = {f['path'] for f in source['files']}
    origins = {}
    for name, module in sorted(sys.modules.items()):
        if name.split('.')[0] not in ('socfai_incentive', 'socfai_cy_v2'):
            continue
        origin = Path(module.__file__).resolve()
        if not origin.is_relative_to(ROOT) or origin.relative_to(ROOT).as_posix() not in approved:
            raise ValueError('import outside approved source: ' + name)
        origins[name] = str(origin)
    return origins


def output_path(value):
    given = Path(value)
    if not given.is_absolute():
        given = ROOT / given
    output = given.resolve()
    base = ROOT / 'outputs'
    if base.resolve() != base or output == base or not output.is_relative_to(base):
        raise ValueError('new directory under repository outputs/ required')
    if any(p.is_symlink() for p in [given, *given.parents]):
        raise ValueError('symlink output forbidden')
    if output.exists():
        raise ValueError('output exists; overwrite forbidden')
    return output


def validate_result(model, mode, result, output, reference):
    rows = result['coalition_results']
    seeds = reference['seeds']
    coalitions = ('NONE', 'C', 'T', 'R', 'CT', 'CR', 'TR', 'CTR') if model == 'dispatch' else (
        'NONE', 'C', 'Y', 'R', 'CY', 'CR', 'YR', 'CYR')
    if len(rows) != reference['coalition_count'] or {r['seed'] for r in rows} != set(seeds):
        raise ValueError('incomplete run set or invalid seed')
    if any(type(r['seed']) is not int for r in rows):
        raise ValueError('invalid seed type')
    for seed in seeds:
        group = [r for r in rows if r['seed'] == seed]
        if len(group) != 8 or {r['coalition_id'] for r in group} != set(coalitions):
            raise ValueError('incomplete coalition set')
    if not all(r['status'] == r['optimizer_status'] == 'success' and r['failed_jobs'] == 0
               and r.get('unassigned_jobs', 0) == 0 for r in rows):
        raise ValueError('invalid seed result')
    if mode == 'formal':
        if (not result['summary']['formal_run_set_valid']
                or result['summary']['valid_seed_count'] != 100
                or not result['validation_report']['formal_run_set_valid']):
            raise ValueError('formal 100-seed validation failed')
    elif not result['summary']['valid']:
        raise ValueError('smoke validation failed')
    digest = sha((output / 'scientific.json').read_bytes())
    if digest != reference['scientific_sha256']:
        raise ValueError('scientific hash mismatch')
    names = ['config.json', 'summary.json', 'validation_report.json', 'coalition_results.csv', 'rewards.csv']
    if mode == 'formal':
        names += ['scenarios.jsonl', 'aggregate_contributions.csv', 'bootstrap_ci.json', 'descriptive_stats.json']
    for name in names:
        if not (output / name).is_file() or not (output / name).stat().st_size:
            raise ValueError('incomplete output package: ' + name)
    return digest


def reproduce(model, mode, order, output):
    if (model, mode) not in CONFIGS or order not in ('forward', 'reverse'):
        raise ValueError('invalid model, mode or order')
    source, references = verify_inputs()
    dest = output_path(output)
    raw = checked_file('configs/' + CONFIGS[model, mode])
    config = json.loads(raw)
    seeds = list(range(100)) if mode == 'formal' else [0]
    if config['seeds'] != seeds or config['mode'] != mode:
        raise ValueError('invalid configured seed set or mode')
    reference = next(r for r in references if (r['model'], r['mode']) == (model, mode))
    if reference['seeds'] != seeds or reference['coalition_count'] != len(seeds) * 8:
        raise ValueError('invalid reference conditions')
    check_imports(source)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / 'src'))
    prefix = 'socfai_incentive' if model == 'dispatch' else 'socfai_cy_v2'
    engine = importlib.import_module(prefix + ('.formal' if mode == 'formal' else '.runner'))
    check_imports(source)
    receipt = dict(status='running', tool_version=VERSION, tool_sha256=sha(Path(__file__).read_bytes()),
                   python=platform.python_version(), model=model, mode=mode, order=order,
                   output=str(dest), started_utc=datetime.now(timezone.utc).isoformat(),
                   config_sha256=sha(raw), source_commit=source['source_commit'],
                   source_files=source['files'], reference_sha256=reference['scientific_sha256'],
                   execution_layer='Python API with manifest validation', formal_cli_guard_invoked=False)
    # Atomic reservation refuses empty as well as nonempty existing output directories.
    dest.mkdir(parents=True, exist_ok=False)
    work = dest / 'run'
    try:
        if model == 'dispatch':
            order_ids = tuple(reversed(engine.COALITIONS)) if order == 'reverse' else engine.COALITIONS
            if mode == 'formal':
                result = engine.execute(raw, work, 'public reproduction', source['source_commit'], order_ids)
            else:
                result = engine.run_seed(config, order=order_ids)
                engine.write_package(work, config, result, 'public reproduction')
        elif mode == 'formal':
            result = engine.execute(raw, work, source['source_commit'], order == 'reverse')
        else:
            result = engine.execute(config, work, order == 'reverse')
        digest = validate_result(model, mode, result, work, reference)
        verify_inputs()
        receipt.update(status='passed', scientific_sha256=digest, hash_match=True,
                       valid_seed_count=len(seeds), coalition_count=len(result['coalition_results']),
                       import_paths=check_imports(source))
    except Exception as exc:
        receipt.update(status='failed', hash_match=False, error=str(exc))
        raise
    finally:
        receipt['finished_utc'] = datetime.now(timezone.utc).isoformat()
        (dest / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2)+'\n')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', choices=('dispatch', 'cy'), required=True)
    parser.add_argument('--mode', choices=('smoke', 'formal'), required=True)
    parser.add_argument('--order', choices=('forward', 'reverse'), required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        receipt = reproduce(args.model, args.mode, args.order, args.output)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps(dict(status='failed', error=str(exc)), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
