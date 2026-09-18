"""Run one router against the sealed evaluation set.

    python scripts/run_router_eval.py                          # keyword baseline
    python scripts/run_router_eval.py --by-category --no-write
    python scripts/run_router_eval.py --provider openai --dry-run
    python scripts/run_router_eval.py --provider openai --confirm-send --budget-usd 0.50

Adding a provider means one entry in PROVIDERS; nothing else in this file or in the
dataset changes. A provider that needs an API key or a running model is expected to
fail loudly here rather than silently degrade.

Results are written under `artifacts/router-eval/`, which is Git-excluded: the dataset
and the runner are the shared asset, per-provider measurements are not.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from router_eval.dataset import DEFAULT_DIR, load_dataset  # noqa: E402
from router_eval.evaluate import evaluate, report_to_dict  # noqa: E402
from router_eval.keyword import KeywordRouter  # noqa: E402
from router_eval.openai_router import DEFAULT_MODEL, BudgetExceeded, OpenAIRouter, api_key_present  # noqa: E402
from router_eval.precheck import estimate, precheck  # noqa: E402

PROVIDERS = {'keyword': KeywordRouter, 'openai': OpenAIRouter}
# Providers that leave this machine. They run only after the precheck passes, the cost
# estimate fits the budget, and --confirm-send is given explicitly.
HOSTED = {'openai'}
OUT_DIR = ROOT / 'artifacts' / 'router-eval'


def build(name, args):
    if name not in PROVIDERS:
        raise SystemExit(f'unknown provider: {name} (available: {", ".join(sorted(PROVIDERS))})')
    if name == 'openai':
        return OpenAIRouter(model=args.model, effort=args.effort, budget_usd=args.budget_usd)
    return PROVIDERS[name]()


def gate(router, dataset, directory):
    """Everything that has to hold before a hosted provider is sent anything."""
    print(f'  api key present : {api_key_present()}')
    findings = precheck(dataset, router, directory)
    print(f'  precheck        : {"PASS" if not findings else "FAIL"} ({len(findings)} findings)')
    for finding in findings[:20]:
        print('    -', finding)
    numbers = estimate(dataset, router)
    print(f'  model           : {router.model}   effort={router.effort}')
    print(f'  requests        : {numbers["requests"]}')
    print(f'  input tokens    : ~{numbers["input_tokens_per_request"]}/request, '
          f'~{numbers["input_tokens_total"]} total (upper bound)')
    print(f'  output tokens   : <={numbers["output_tokens_cap_per_request"]}/request, '
          f'<={numbers["output_tokens_total_worst_case"]} total')
    print(f'  estimated cost  : <=USD {numbers["total_usd_worst_case"]} '
          f'(input {numbers["input_usd"]} + output {numbers["output_usd_worst_case"]})')
    print(f'  budget          : USD {numbers["budget_usd"]}  within_budget={numbers["within_budget"]}')
    return findings, numbers


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run a router against the sealed evaluation set')
    parser.add_argument('--provider', default='keyword', choices=sorted(PROVIDERS))
    parser.add_argument('--dataset', default=None, help='dataset directory (default: fixtures/router-eval-v1)')
    parser.add_argument('--no-write', action='store_true', help='print the summary without writing a result file')
    parser.add_argument('--by-category', action='store_true', help='also print the per-category table')
    parser.add_argument('--model', default=DEFAULT_MODEL, help='model id for a hosted provider')
    parser.add_argument('--effort', default='none', help='reasoning effort for a hosted provider')
    parser.add_argument('--budget-usd', type=float, default=0.50, help='hard spending cap for one run')
    parser.add_argument('--dry-run', action='store_true',
                        help='precheck, estimate and show one request; send nothing')
    parser.add_argument('--confirm-send', action='store_true',
                        help='required to actually call a hosted provider')
    args = parser.parse_args(argv)
    hosted = args.provider in HOSTED
    if args.dry_run and not hosted:
        raise SystemExit(f'--dry-run applies to a hosted provider ({", ".join(sorted(HOSTED))}); '
                         f'{args.provider} runs locally and sends nothing')
    directory = Path(args.dataset) if args.dataset else DEFAULT_DIR
    router = build(args.provider, args)
    dataset = load_dataset(directory)
    if hosted:
        print(f'pre-flight ({args.provider})')
        findings, numbers = gate(router, dataset, directory)
        if args.dry_run:
            print('\nsample request (this exact body would be sent):')
            print(json.dumps(numbers['sample_payload'], ensure_ascii=False, indent=1)[:2400])
            print('\nsent_requests = 0 (dry run)')
            return 0 if not findings else 1
        if findings:
            raise SystemExit('precheck failed; nothing was sent')
        if not numbers['within_budget']:
            raise SystemExit('estimate exceeds the budget; nothing was sent')
        if not api_key_present():
            raise SystemExit('OPENAI_API_KEY is not set; nothing was sent')
        if not args.confirm_send:
            raise SystemExit('refusing to send without --confirm-send')
        print('  sending...\n')
    try:
        report = evaluate(router, dataset)
    except BudgetExceeded as stop:
        raise SystemExit(f'stopped mid-run: {stop}. Partial results are not reported as a run.')
    print('\n'.join(report.summary_lines()))
    if args.by_category:
        print('\n'.join(report.category_lines()))
    data = report_to_dict(report)
    data['seal'] = dataset.seal
    if hasattr(router, 'usage'):
        # Token counts and cost only. No request body, no response body, no credential.
        data['usage'] = dict(router.usage)
        data['cost_usd'] = round(router.cost_usd, 6)
        data['model'] = router.model
        data['effort'] = router.effort
        print(f'\n  usage: {router.usage}  cost: USD {round(router.cost_usd, 6)}')
    if args.no_write:
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = OUT_DIR / f'{args.provider}-{stamp}.json'
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f'\nwrote {path.relative_to(ROOT)} (Git-excluded)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
