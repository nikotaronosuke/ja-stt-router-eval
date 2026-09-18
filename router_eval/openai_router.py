"""OpenAIRouter: an evaluation-only router backed by the OpenAI Responses API.

This is for measuring a hosted provider against the sealed question set. Like
every router in this package it returns a candidate id or nothing: the JSON
schema it constrains the model to has no field for a sentence, so the model has
no place to put an answer even if it tried, and the instructions say so.

What leaves this machine, per request: the current question, the previous
question when there is one, and the id, title and search terms of each
candidate. The candidates' descriptions and the evaluation labels never enter
the payload; `tests/router/test_openai_router.py` asserts that against a built
request, and `precheck` inspects every request of a run before anything is sent.

Transport is `urllib` from the standard library, so this adds no dependency.
The API key is read from `OPENAI_API_KEY` for each request, is never stored on
the instance, never logged and never included in an error message.

Retention: every request sets `store: false`, so no response is kept as
application state. That is not zero data retention; provider-side abuse
monitoring retention is governed by the provider's own policy, which the
operator checks before a run.

Scores are this provider's own. `openai_relevance_0_1` is a number the model
reports for how well a candidate fits the question; it is not a calibrated
probability and it is not comparable with the keyword router's match count.

Model id and prices are configuration, not measurements. The defaults below
were the values used when this module was written; check the provider's current
model list and pricing page before a run, and pass overrides on the CLI.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .contract import Router, ScoredCandidate

ENDPOINT = 'https://api.openai.com/v1/responses'
DEFAULT_MODEL = 'gpt-5.6-luna'
DEFAULT_PRICES_USD_PER_MILLION = {'input': 0.20, 'output': 1.20, 'cached': 0.02}
EVAL_TIMEOUT_S = 60
SCHEMA_NAME = 'router_decision'
MAX_ALTERNATIVES = 3
# Sized for the JSON decision alone, which is valid because effort 'none' produces
# no reasoning tokens. Any other effort needs the larger cap or the reply can truncate.
OUTPUT_TOKENS = {'none': 256}
OUTPUT_TOKENS_DEFAULT = 2048

# No field here can carry prose. Adding one would be a change to this file, which
# the contract test and the router tests both guard.
DECISION_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'selected_id': {
            'type': ['string', 'null'],
            'description': '最も関連する候補のID。該当なし・判断できない場合は null。',
        },
        'alternatives': {
            'type': 'array',
            'maxItems': MAX_ALTERNATIVES,
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'candidate_id': {'type': 'string'},
                    'score': {'type': 'number', 'description': '0.0〜1.0の関連度。確率ではない。'},
                },
                'required': ['candidate_id', 'score'],
            },
        },
        'abstain': {'type': 'boolean', 'description': '候補を選ばないことが正しいなら true。'},
        'review': {'type': 'boolean', 'description': '選んだが確信が低く、人の確認が望ましいなら true。'},
    },
    'required': ['selected_id', 'alternatives', 'abstain', 'review'],
}

INSTRUCTIONS = (
    'あなたは短い発話を、限られた候補IDへ振り分ける照合器です。発話と候補の一覧を受け取り、'
    '「どの候補が該当するか／該当なし」だけを判定します。\n'
    '\n'
    '禁止: 回答文・説明文・要約・言い換えなど、人が読む文章を一切作らないでください。'
    '出力は指定された JSON のみで、文章を入れる場所はありません。\n'
    '\n'
    '各候補は id、タイトル、関連語だけで与えられます。候補の本文は渡されません。'
    '与えられた情報から該当すると判断できない場合は、推測で補わないでください。\n'
    '\n'
    '次のいずれかに当たるときは、無理に候補を選ばず abstain を true にしてください。'
    '誤った候補を出すことは、何も出さないことより悪い結果になります。\n'
    '- 根拠が弱く、どの候補とも十分に結びつかない\n'
    '- 発話が挨拶・雑談・進行上の発言・感想であり、案内を求めていない\n'
    '- 候補の語が発話に出てくるだけで、発話の意味は別のことを指している\n'
    '- 候補が扱う範囲の外（別の施設・提供していないサービス・事実と異なる前提）を尋ねている\n'
    '- 複数の解釈があり、発話者にしか選べない\n'
    '\n'
    '発話が明確に複数の候補に当たる場合は、最も中心的なものを selected_id にし、'
    '残りを alternatives に入れてください。abstain のときは selected_id を null にします。\n'
    '\n'
    '直前の発話が与えられた場合は、「具体的には？」のような短い追加質問の文脈を補うためだけに'
    '使ってください。直前の発話から新しい事実を作らないでください。\n'
    '\n'
    'score は 0.0〜1.0 の関連度で、確率ではありません。'
    'review は、選んだが確信が低く人の確認が望ましい場合に true にします。'
)


class BudgetExceeded(RuntimeError):
    """Raised before a request that would push the run past its spending cap."""


class MissingApiKey(RuntimeError):
    """OPENAI_API_KEY is not set. Raised before any network access is attempted."""


def api_key_present():
    """True/False only. The value is never returned, printed or stored."""
    return bool(os.environ.get('OPENAI_API_KEY', '').strip())


def _api_key():
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not key:
        raise MissingApiKey('OPENAI_API_KEY_missing')
    return key


def candidate_lines(views):
    """The only candidate information that leaves this machine: id, title, search terms."""
    return '\n'.join(f'[{view.id}] タイトル: {view.title} / 関連語: {"、".join(view.triggers)}' for view in views)


def build_input(request):
    previous = request.previous_question.strip() if request.previous_question else ''
    return ('現在の発話: ' + str(request.question).strip() + '\n'
            '直前の発話: ' + (previous if previous else '（なし）') + '\n\n'
            '候補一覧:\n' + candidate_lines(request.candidates))


def build_payload(request, model=DEFAULT_MODEL, effort='none'):
    """The exact JSON body that would be sent.

    Built without touching the network so a dry run can inspect and assert on it.
    """
    return {
        'model': model,
        'instructions': INSTRUCTIONS,
        'input': build_input(request),
        'text': {'format': {'type': 'json_schema', 'name': SCHEMA_NAME,
                            'schema': DECISION_SCHEMA, 'strict': True}},
        'reasoning': {'effort': effort},
        'store': False,  # no application state; provider-side retention is separate
        'stream': False,
        'max_output_tokens': OUTPUT_TOKENS.get(effort, OUTPUT_TOKENS_DEFAULT),
    }


def _post(payload, timeout_s):
    """One Responses API call.

    Errors are reduced to a status and a short reason so no request body,
    response body or credential can reach a log.
    """
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    http = urllib.request.Request(ENDPOINT, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + _api_key(), 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(http, timeout=timeout_s) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'openai_http_{error.code}') from None
    except urllib.error.URLError:
        raise RuntimeError('openai_unreachable') from None


def extract_text(response):
    """The structured-output JSON string from a Responses API reply."""
    if response.get('status') == 'incomplete':
        reason = (response.get('incomplete_details') or {}).get('reason', 'unknown')
        raise RuntimeError('openai_incomplete_' + str(reason))
    output_text = response.get('output_text')
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    for item in response.get('output') or []:
        for part in item.get('content') or []:
            if part.get('type') == 'refusal':
                raise RuntimeError('openai_refusal')
            text = part.get('text')
            if isinstance(text, str) and text.strip():
                return text
    raise RuntimeError('openai_no_output')


class OpenAIRouter(Router):
    """Evaluation-only hosted router."""
    name = 'openai'
    score_type = 'openai_relevance_0_1'
    decision_policy = ('structured classification over candidate views; the model reports abstain '
                       'and the router abstains as well on any unknown or inconsistent id')
    threshold = None  # no post-hoc cut-off: adding one would be tuning between runs

    def __init__(self, model=DEFAULT_MODEL, effort='none', budget_usd=0.50, timeout_s=EVAL_TIMEOUT_S,
                 transport=None, prices=None):
        self.model = model
        self.effort = effort
        self.budget_usd = float(budget_usd)
        self.timeout_s = timeout_s
        # Injected in tests so the suite never opens a socket.
        if transport is not None:
            self.transport = transport
        else:
            self.transport = lambda payload: _post(payload, self.timeout_s)
        self.prices = dict(DEFAULT_PRICES_USD_PER_MILLION)
        if prices:
            self.prices.update(prices)
        self.usage = {'requests': 0, 'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0}
        self.cost_usd = 0.0

    def _account(self, response):
        usage = response.get('usage') or {}
        total_input = int(usage.get('input_tokens') or 0)
        cached = int(usage.get('cache_read_input_tokens') or 0)
        output = int(usage.get('output_tokens') or 0)
        fresh = max(total_input - cached, 0)
        self.usage['requests'] += 1
        self.usage['input_tokens'] += total_input
        self.usage['cached_input_tokens'] += cached
        self.usage['output_tokens'] += output
        self.cost_usd += (fresh * self.prices['input'] + cached * self.prices['cached']
                          + output * self.prices['output']) / 1_000_000

    def decide(self, request):
        if self.cost_usd >= self.budget_usd:
            raise BudgetExceeded(f'budget_reached_after_{self.usage["requests"]}_requests')
        payload = build_payload(request, self.model, self.effort)
        response = self.transport(payload)
        self._account(response)
        data = json.loads(extract_text(response))
        known = {view.id for view in request.candidates}
        alternatives = []
        for item in data.get('alternatives') or []:
            candidate_id = item.get('candidate_id')
            already = any(candidate_id == existing.candidate_id for existing in alternatives)
            if candidate_id in known and not already:
                alternatives.append(ScoredCandidate(candidate_id=candidate_id,
                                                    score=float(item.get('score') or 0.0)))
        selected = data.get('selected_id')
        # Abstain on anything inconsistent: an unknown id, or a selection made while the
        # model also said to abstain. Never guess a candidate the fixture does not hold.
        if data.get('abstain') or not isinstance(selected, str) or selected not in known:
            selected = None
        metadata = {'review': bool(data.get('review')), 'effort': self.effort, 'candidates': len(alternatives)}
        return selected, alternatives[:MAX_ALTERNATIVES], metadata
