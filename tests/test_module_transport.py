"""Catch missing/cross-wired switches through each real Shio consumer."""
import unittest
from types import SimpleNamespace

from test_name_semantic_provider_routing import MAIN
from test_r14_reviewer_regressions import R14LifecycleAndPresentationTests as Fixtures, _AuxiliaryEvent


class Provider:
    provider_config = {"id": "r14-chat"}

    def __init__(self, streaming, content):
        self.streaming = streaming
        self.content = content

    async def text_chat(self, *, prompt, func_tool):
        assert func_tool is None
        return SimpleNamespace(completion_text="invalid" if self.streaming else self.content)

    async def text_chat_stream(self, *, prompt, func_tool):
        assert func_tool is None
        yield SimpleNamespace(is_chunk=True, role="assistant", completion_text="discard this delta")
        yield SimpleNamespace(is_chunk=False, role="assistant", completion_text=self.content if self.streaming else "invalid")


class ModuleTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_module_uses_its_own_switch_in_both_directions(self):
        for purpose, content in [('name', '{"decision":"DIRECT"}'), ('natural', '{"decision":"REPLY"}'), ('review', '{"action":"keep"}'), ('segment', '{"segments":["alpha","beta"]}')]:
            for streaming in (True, False):
                with self.subTest(purpose=purpose, streaming=streaming):
                    provider = Provider(streaming, content)
                    p = Fixtures()._auxiliary_plugin(provider, explicit_route=True, group={
                        "name_wake_mode": "semantic", "name_semantic_provider_id": "r14-chat",
                        "natural_decision_provider_id": "r14-chat",
                        "name_semantic_streaming": not streaming,
                        "natural_decision_streaming": not streaming,
                    }, review={"mode":"core", "provider_id":"r14-chat", "streaming":not streaming})
                    presentation = {"model_segment_provider_id":"r14-chat", "model_segment_streaming":not streaming}
                    p.config['sys001']['presentation'] = presentation
                    p.config['sys001']['model_transport'] = {"stream_provider_ids":[] if streaming else ["r14-chat"]}
                    event = _AuxiliaryEvent("Shio, hello")
                    snapshot = MAIN.create_snapshot(event, origin="natural")
                    if purpose == 'name':
                        p.config['sys001']['group']['name_semantic_streaming'] = streaming
                        self.assertTrue(await p._visible_name_wake(event))
                    elif purpose == 'natural':
                        p.config['sys001']['group']['natural_decision_streaming'] = streaming
                        self.assertEqual('REPLY', await p._decide_natural_participation(event, snapshot))
                    elif purpose == 'review':
                        p.config['sys001']['final_review']['streaming'] = streaming
                        outcome = await p._review_final_text(event, snapshot, 'original text')
                        self.assertFalse(outcome.exhausted)
                        self.assertEqual('original text', outcome.text)
                    else:
                        presentation['model_segment_streaming'] = streaming
                        self.assertEqual(['alpha','beta'], await p._model_text_components(event, 'alphabeta', presentation))
