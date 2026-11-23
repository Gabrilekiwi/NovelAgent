from __future__ import annotations

import ipaddress
import re
from threading import Lock
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from core.config import get_config
from core.writing_profile import ProfileError, read_profile, save_profile


def endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('请填写有效的服务地址。')
    value = value.strip().rstrip('/')
    if not value:
        return ''
    try:
        url = urlsplit(value)
        host = url.hostname
        _ = url.port
        local = host == 'localhost'
        if host and not local:
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or url.username is not None or url.password is not None or '?' in value or '#' in value
                or re.search(r'[\s\\\x00-\x1f\x7f]', value) or not value.isascii()
                or not (url.scheme == 'https' or (local and url.scheme == 'http'))):
            raise ValueError
    except ValueError:
        raise ValueError('服务地址须为 HTTPS（本机服务可用 HTTP），不能包含密钥、查询参数或片段。') from None
    return value


def install_provider_settings(app, path):
    lock = Lock()

    def form_state():
        saved = read_profile(path)
        if saved:
            return dict(model=saved['model'], base_url=saved['base_url'], has_key=True, source='workbench')
        cfg = get_config()
        try:
            url = endpoint(cfg.openai_base_url or '')
        except ValueError:
            url = ''  # Never return credentials embedded in an old environment URL.
        return dict(model=cfg.openai_model, base_url=url, has_key=bool(cfg.openai_api_key), source='environment' if cfg.openai_api_key else 'none')

    def fail(message, status=422):
        return JSONResponse({'error': {'code': 'writing_model_settings', 'message': message}}, status_code=status)

    @app.get('/api/settings/writing-model')
    async def model_settings():
        try:
            return form_state()
        except ProfileError:
            return dict(model='', base_url='', has_key=False, source='unreadable')

    @app.put('/api/settings/writing-model')
    async def edit_model(request: Request):
        if request.headers.get('content-type','').split(';')[0].strip() != 'application/json':
            return fail('请从模型设置表单保存。', 415)
        try:
            body = await request.json()
        except ValueError:
            return fail('无法读取模型设置。')
        if not isinstance(body, dict) or set(body) != {'base_url','model','api_key'} or not all(isinstance(x,str) for x in body.values()):
            return fail('模型设置格式不正确。')
        model, key = body['model'].strip(), body['api_key'].strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}',model):
            return fail('请填写服务商提供的模型标识。')
        if len(key)>8192 or (key and not re.fullmatch(r'[!-~]+', key)):
            return fail('密钥不能包含空格或控制字符。')
        try:
            url = endpoint(body['base_url'])
            with lock:
                if not key:
                    # Check actual destination, including the path, before reusing any key.
                    existing = read_profile(path)
                    if existing:
                        old_url, key = existing['base_url'], existing['api_key']
                    else:
                        cfg = get_config(); old_url, key = cfg.openai_base_url or '', cfg.openai_api_key
                    if not key or endpoint(old_url) != url:
                        return fail('首次配置或更换服务地址时，请填写该服务的密钥。')
                save_profile(path, dict(model=model, base_url=url, api_key=key))
                return form_state()
        except (ProfileError, ValueError) as exc:
            return fail(str(exc))
