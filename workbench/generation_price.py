"""Local, optional USD scenarios for one whole-chapter request (never budget authority)."""
import argparse
import hashlib
import hmac
import json
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from tempfile import NamedTemporaryFile

from api.budget_endpoint import endpoint_fingerprint
from core.chapter_budget_store import ChapterBudgetError
from core.config import get_config

PROFILE = Path(__file__).resolve().parents[1] / '.novelagent-chapter-price.local.json'


def _binding(cfg):
    if not cfg.openai_api_key:
        return None
    endpoint = endpoint_fingerprint('openai', cfg.openai_base_url or 'https://api.openai.com/v1')
    return hmac.new(cfg.openai_api_key.encode(), f'chapter-quote-v1:{endpoint}:{cfg.openai_model}'.encode(), hashlib.sha256).hexdigest()


def _rate(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,10})?", value):
        raise ValueError('invalid unit rate')
    rate = Decimal(value)
    if rate > 1_000_000:
        raise ValueError('invalid unit rate')
    return rate


def _check_profile(p):
    if not isinstance(p, dict) or p.get('version') != 1:
        raise ValueError('unsupported local price profile')
    date.fromisoformat(p['as_of'])
    if type(p['input_tier_limit']) is not int or p['input_tier_limit'] <= 0:
        raise ValueError('invalid input tier')
    return _rate(p['input_usd_per_million']), _rate(p['output_usd_per_million'])


def scenario(input_tokens, output_tokens, input_rate, output_rate):
    amount = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    return str(amount.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP))


def generation_quote(input_tokens):
    cfg = get_config()
    output = cfg.openai_max_output_tokens
    result = {'model': cfg.openai_model, 'input_tokens_estimate': input_tokens,
              'output_token_limit': output if output > 0 else None, 'price': None}
    try:
        p = json.loads(PROFILE.read_text(encoding='utf-8'))
        ir, out = _check_profile(p)
        binding = _binding(cfg)
        if (binding is None or not hmac.compare_digest(binding, p['binding']) or
                input_tokens > p['input_tier_limit'] or output <= 0):
            return result
    except (KeyError, ValueError, TypeError, OSError, InvalidOperation, ChapterBudgetError):
        return result
    example = min(4000, output)
    result['price'] = {'as_of': p['as_of'], 'currency': 'USD', 'input_usd_per_million': str(ir),
        'output_usd_per_million': str(out), 'example_output_tokens': example,
        'example_usd': scenario(input_tokens, example, ir, out),
        'at_output_limit_usd': scenario(input_tokens, output, ir, out)}
    return result


def main():
    parser = argparse.ArgumentParser(description='Save local USD rates for the configured chapter model, key and endpoint.')
    parser.add_argument('--model', required=True, help='must equal the configured model')
    parser.add_argument('--as-of', required=True, help='rate observation date, YYYY-MM-DD')
    parser.add_argument('--input-rate', required=True, help='USD per million ordinary input tokens')
    parser.add_argument('--output-rate', required=True, help='USD per million output tokens')
    parser.add_argument('--input-tier-limit', type=int, required=True)
    args = parser.parse_args()
    cfg = get_config()
    if cfg.openai_model != args.model or not cfg.openai_api_key:
        parser.error('Configure the matching model, endpoint and key first.')
    try:
        profile = dict(version=1, binding=_binding(cfg), as_of=args.as_of, input_tier_limit=args.input_tier_limit,
                       input_usd_per_million=args.input_rate, output_usd_per_million=args.output_rate)
        _check_profile(profile)
    except (ValueError, InvalidOperation, ChapterBudgetError) as exc:
        parser.error(str(exc))
    with NamedTemporaryFile('w', encoding='utf-8', dir=PROFILE.parent, delete=False) as f:
        tmp = Path(f.name)
        json.dump(profile, f, indent=2)
    try:
        os.replace(tmp, PROFILE)
    finally:
        tmp.unlink(missing_ok=True)
    print('Local price scenario saved. No provider request was sent.')


if __name__ == '__main__':
    main()
