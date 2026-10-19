'''Shio 设置页 Web API。

仅依赖标准库与 astrbot.api 的 logger；astrbot.api.web 在注册时按需导入，
这样旧版 AstrBot（4.27.4）即使没有该模块也能正常加载插件。
'''

import copy
import hashlib
import importlib
import json
import math
from pathlib import Path

from astrbot.api import logger

_PLUGIN_NAME = 'astrbot_plugin_shio'
_SCHEMA_PATH = Path(__file__).resolve().parents[1] / '_conf_schema.json'
_SCHEMA_CACHE = None

_STRING_TYPES = ('string', 'text')
_LIST_ITEM_TYPES = ('string', 'text')


def _load_schema():
    '''读取并校验 _conf_schema.json；失败时显式抛出异常。'''
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    with _SCHEMA_PATH.open('r', encoding='utf-8') as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError('settings schema must be a JSON object')
    sys001 = loaded.get('sys001')
    if not isinstance(sys001, dict) or sys001.get('type') != 'object':
        raise ValueError('settings schema requires a sys001 object')
    items = sys001.get('items')
    if not isinstance(items, dict) or not items:
        raise ValueError('settings schema requires non-empty sys001.items')
    _SCHEMA_CACHE = loaded
    return _SCHEMA_CACHE


def _collect_leaves(schema):
    '''把 schema 展开成 {点分路径: 叶子定义}。'''
    leaves = {}

    def walk(node, prefix):
        if not isinstance(node, dict):
            return
        for key, spec in node.items():
            if not isinstance(key, str) or not isinstance(spec, dict):
                continue
            path = prefix + key
            nested = spec.get('items')
            if spec.get('type') == 'object' and isinstance(nested, dict):
                walk(nested, path + '.')
            else:
                leaves[path] = spec

    walk(schema, '')
    return leaves


def _plain(value):
    '''返回配置的深拷贝快照，不做任何类型转换。'''
    return copy.deepcopy(dict(value))


def _revision(snapshot):
    encoded = json.dumps(
        snapshot,
        sort_keys=True,
        ensure_ascii=False,
        separators=(',', ':'),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _safe_revision(snapshot):
    try:
        return _revision(snapshot)
    except (TypeError, ValueError) as exc:
        logger.error('settings revision failed: %s', type(exc).__name__)
        return None


def _options_ok(options, value):
    if isinstance(value, bool):
        return any(isinstance(option, bool) and option == value for option in options)
    return any((not isinstance(option, bool)) and option == value for option in options)


def _value_ok(spec, value):
    '''只校验本次提交的值，未改动的历史值保持原样。'''
    kind = spec.get('type')
    options = spec.get('options')
    if isinstance(options, list) and options and not _options_ok(options, value):
        return False
    if kind in _STRING_TYPES:
        return isinstance(value, str)
    if kind == 'bool':
        return isinstance(value, bool)
    if kind == 'int':
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == 'float':
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return math.isfinite(value)
    if kind == 'list':
        if not isinstance(value, list):
            return False
        if not all(isinstance(item, str) for item in value):
            return False
        items = spec.get('items')
        if isinstance(items, dict):
            item_type = items.get('type')
            if item_type is not None and item_type not in _LIST_ITEM_TYPES:
                return False
        return True
    return False


def _assign(target, path, value):
    '''按点分路径写入合并候选对象。'''
    parts = path.split('.')
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = copy.deepcopy(value)


def _restore(config, original):
    '''save_config 抛错后把注入对象恢复成保存前快照。'''
    try:
        config.clear()
        config.update(copy.deepcopy(original))
    except Exception as exc:
        logger.error('settings restore failed: %s', type(exc).__name__)


def register_settings_page(context):
    '''注册设置页路由；旧版 AstrBot 缺少 astrbot.api.web 时返回 False。'''
    register = getattr(context, 'register_web_api', None)
    if not callable(register):
        return False

    try:
        web_api = importlib.import_module('astrbot.api.web')
    except ModuleNotFoundError as exc:
        if getattr(exc, 'name', None) == 'astrbot.api.web':
            return False
        raise

    class _SettingsController:
        '''独立控制器：只持有 context 与 schema，配置每次请求重新解析。'''

        def __init__(self, ctx, schema):
            self._context = ctx
            self._schema = schema

        def _state(self):
            metadata = self._context.get_registered_star(_PLUGIN_NAME)
            if metadata is None:
                return None, web_api.error_response('插件未加载', status_code=404)
            if not getattr(metadata, 'activated', False):
                return None, web_api.error_response('插件未启用', status_code=403)
            if getattr(metadata, 'star_cls', None) is None:
                return None, web_api.error_response('插件未就绪', status_code=403)
            config = getattr(metadata, 'config', None)
            if config is None:
                return None, web_api.error_response('配置不可用', status_code=503)
            return (metadata, config), None

        async def get_settings(self):
            state, failure = self._state()
            if failure is not None:
                return failure
            metadata, config = state
            snapshot = _plain(config)
            revision = _safe_revision(snapshot)
            if revision is None:
                return web_api.error_response('配置读取失败', status_code=500)
            version = getattr(metadata, 'version', '')
            return web_api.json_response(
                {
                    'schema': self._schema,
                    'config': snapshot,
                    'revision': revision,
                    'version': version if isinstance(version, str) else '',
                }
            )

        async def post_settings(self):
            payload = await web_api.request.json(default=None)
            if not isinstance(payload, dict):
                return web_api.error_response('请求格式无效', status_code=400)
            revision = payload.get('revision')
            changes = payload.get('changes')
            if not isinstance(revision, str) or not isinstance(changes, dict):
                return web_api.error_response('请求格式无效', status_code=400)

            state, failure = self._state()
            if failure is not None:
                return failure
            metadata, config = state
            current = _plain(config)
            current_revision = _safe_revision(current)
            if current_revision is None:
                return web_api.error_response('配置读取失败', status_code=500)
            if current_revision != revision:
                return web_api.error_response('配置已变更，请刷新后重试', status_code=409)

            leaves = _collect_leaves(self._schema)
            candidate = copy.deepcopy(current)
            for path, value in changes.items():
                if not isinstance(path, str):
                    return web_api.error_response('设置项无效', status_code=400)
                spec = leaves.get(path)
                if spec is None:
                    return web_api.error_response('设置项无效', status_code=400)
                if not _value_ok(spec, value):
                    return web_api.error_response('设置值无效', status_code=400)
                _assign(candidate, path, value)

            original = copy.deepcopy(current)
            try:
                config.save_config(candidate)
            except Exception as exc:
                _restore(config, original)
                logger.error('settings save failed: %s', type(exc).__name__)
                return web_api.error_response('保存失败，原配置已保留', status_code=500)

            snapshot = _plain(config)
            new_revision = _safe_revision(snapshot)
            if new_revision is None:
                return web_api.error_response('配置读取失败', status_code=500)
            return web_api.json_response(
                {'config': snapshot, 'revision': new_revision}
            )

        async def get_models(self):
            _, failure = self._state()
            if failure is not None:
                return failure
            try:
                providers = self._context.get_all_providers()
            except Exception as exc:
                logger.error('settings providers failed: %s', type(exc).__name__)
                return web_api.error_response('模型列表不可用', status_code=503)
            items = []
            for provider in providers or []:
                try:
                    meta = provider.meta()
                    provider_id = getattr(meta, 'id', None)
                    model = provider.get_model()
                except Exception as exc:
                    logger.error('settings provider failed: %s', type(exc).__name__)
                    continue
                if isinstance(provider_id, str) and isinstance(model, str):
                    items.append({'id': provider_id, 'model': model})
            return web_api.json_response({'providers': items})

    controller = _SettingsController(context, _load_schema())
    register(
        '/astrbot_plugin_shio/settings',
        controller.get_settings,
        ['GET'],
        '读取 Shio 自然观察插件设置',
    )
    register(
        '/astrbot_plugin_shio/settings',
        controller.post_settings,
        ['POST'],
        '保存 Shio 自然观察插件设置',
    )
    register(
        '/astrbot_plugin_shio/models',
        controller.get_models,
        ['GET'],
        '列出可供 Shio 使用的文本模型',
    )
    return True
