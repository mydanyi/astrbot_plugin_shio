"""Real QQ Pipeline + Shio/Meme, external HTTP enforces selected transport."""
import asyncio
import base64
import json
from pathlib import Path

import qq_delivery as q


class Endpoint(q.Endpoint):
    required_stream = False
    mismatches = 0

    async def handle(self, request):
        payload = await request.json()
        if bool(payload.get("stream", False)) != self.required_stream:
            self.mismatches += 1
            return q.web.json_response({"error": {"message": "wrong transport", "type": "invalid_request_error"}}, status=400)
        return await super().handle(request)


async def main():
    await q.sp.initialize()
    config = q.settings()
    config["provider_settings"]["unsupported_streaming_strategy"] = "turn_off"
    Path("data/config").mkdir(parents=True, exist_ok=True)
    Path("data/config/astrbot_plugin_shio_config.json").write_text(json.dumps({"sys001": {
        "ingress": {"group_allowed_scopes": ["10001"], "private_allowed_sender_ids": ["101"]},
        "group": {"continuous_window_enabled": False, "natural_participation_enabled": False},
        "presentation": {"text_component_mode": "plugin", "text_component_min_segments": 1,
                         "text_component_max_segments": 3, "bubble_send_min_wait_seconds": 0, "bubble_send_max_wait_seconds": 0},
        "final_review": {"mode": "off"},
        "master_alert": {"master_alert_enabled": True, "review_repair_exhausted_enabled": True, "master_alert_quiet_enabled": False}
    }}))
    image_dir = Path("data/plugin_data/meme_manager/packs/builtin-default/memes/happy")
    image_dir.mkdir(parents=True, exist_ok=True)
    image_dir.joinpath("fixture.png").write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/zg7WAAAAAElFTkSuQmCC"))
    Path("data/config/meme_manager_config.json").write_text(json.dumps({"generation": {"trigger": {"scope": "chat_and_plugin_llm"}, "emotion": {"llm": {"enabled": False}}, "message": {"enable_mixed": False}}, "semantic": {"enabled": False}}))
    queue = asyncio.Queue()
    adapter = q.AiocqhttpAdapter({"id": "fixture-qq", "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 18081}, config["platform_settings"], queue)
    bot_task = asyncio.create_task(adapter.bot.run_task("127.0.0.1", 18081))
    endpoint = Endpoint()
    app = q.web.Application()
    app.router.add_post("/v1/chat/completions", endpoint.handle)
    server = q.web.AppRunner(app)
    await server.setup()
    await q.web.TCPSite(server, "127.0.0.1", 18082).start()
    provider = q.ProviderOpenAIOfficial({"id": "fixture", "type": "openai_chat_completion", "key": ["fixture-key"], "model": "fixture", "api_base": "http://127.0.0.1:18082/v1", "max_context_tokens": 0}, config["provider_settings"])
    manager = None
    try:
        context = q.FixtureContext(config, provider, adapter, queue)
        manager = q.PluginManager(context, config)
        for name in ("meme_manager", "astrbot_plugin_shio", "astrbot"):
            loaded, error = await manager.load(specified_dir_name=name)
            assert loaded, error
        shio = next(s.star_cls for s in q.star_registry if s.root_dir_name == "astrbot_plugin_shio")
        pc = q.PipelineContext(astrbot_config=config, plugin_manager=manager, astrbot_config_id="fixture")
        scheduler = q.PipelineScheduler(pc)
        scheduler.stages = [q.WakingCheckStage(), q.ProcessStage(), q.ResultDecorateStage(), q.RespondStage()]
        for stage in scheduler.stages:
            await stage.initialize(pc)
        async with await q.peer_connect() as ws:
            captures = []
            capture_task = asyncio.create_task(q.capture_sends(ws, captures))
            try:
                # Bind owner's real private event for subsequent timeout notifications.
                endpoint.responses = [{"role": "assistant", "content": "绑定完成。"}]
                event = await q.event_from_qq(adapter, private=True, number=500, sender="100")
                await asyncio.wait_for(scheduler.execute(event), 15)
                for stream in (False, True):
                    endpoint.required_stream = stream
                    config["provider_settings"]["streaming_response"] = stream
                    await scheduler.stages[1].initialize(pc)
                    # Opposite legacy mode ensures the module switch owns routing.
                    shio.config["sys001"]["model_transport"] = {"stream_provider_ids": [] if stream else ["fixture"]}
                    shio.config["sys001"]["final_review"]["streaming"] = stream
                    shio.config["sys001"]["final_review"].update({"mode": "core", "provider_id": "fixture", "timeout_seconds": 5, "max_repair_attempts": 0, "repair_enabled": False, "send_last_reply_on_review_exhausted": False})
                    for count, private, tool in [(1, False, False), (2, True, False), (3, False, True), (3, True, True)]:
                        captures.clear()
                        endpoint.requests.clear()
                        text = "\n".join(["第一句。", "第二句。", "第三句。"][:count])
                        draft = {"role": "assistant", "content": "不能发出的工具草稿。", "tool_calls": [{"id": "lookup1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}
                        endpoint.responses = ([draft] if tool else []) + [{"role": "assistant", "content": text + "\n&&happy&&"}, {"role": "assistant", "content": '{"action":"keep"}'}]
                        event = await q.event_from_qq(adapter, private=private, number=510+count+10*int(private)+30*int(stream), sender="100", text="今天真开心。")
                        await asyncio.wait_for(scheduler.execute(event), 25)
                        bubbles = q.text_bubbles(captures)
                        images = [c for p in captures for c in p.get("params", {}).get("message", []) if c.get("type") == "image"]
                        assert bubbles == text.splitlines(), (stream, private, bubbles)
                        assert len(images) == 1, (stream, private, len(images))
                        assert len(endpoint.requests) == (3 if tool else 2), len(endpoint.requests)
                        assert not endpoint.responses and not endpoint.mismatches
                        assert all(bool(r.get("stream")) == stream for r in endpoint.requests)
                        print("CASE " + json.dumps({"stream": stream, "private": private, "bubbles": count, "tool": tool, "images": len(images), "ok": True}), flush=True)
                    captures.clear()
                    endpoint.requests.clear()
                    getattr(shio, "_master_alert_recent", {}).clear()
                    # Each mode tests its first fault. Do not carry the previous
                    # mode's already-submitted record into the deduplication window.
                    shio._master_alert_record = type(shio._master_alert_record)(
                        master_umo=shio._master_alert_record.master_umo)
                    shio.config["sys001"]["final_review"]["timeout_seconds"] = 0.04
                    endpoint.responses = [{"role": "assistant", "content": "尚未通过审核。"}, {"role": "assistant", "content": '{"action":"keep"}', "_fixture_delay": 0.3}]
                    event = await q.event_from_qq(adapter, private=False, number=590+int(stream), sender="101")
                    await asyncio.wait_for(scheduler.execute(event), 10)
                    await asyncio.sleep(0.4)
                    assert event.get_extra("shio.sys001.review_reason") == "timeout"
                    assert len(endpoint.requests) == 2 and not endpoint.responses
                    assert not endpoint.mismatches
                    assert all(bool(r.get("stream")) == stream for r in endpoint.requests)
                    sends = [p for p in captures if p.get("action", "").startswith("send_")]
                    assert sends and all(p.get("params", {}).get("user_id") in (100, "100") for p in sends), sends
                    assert not any(p.get("params", {}).get("group_id") for p in sends), sends
                    assert "尚未通过审核" not in json.dumps(sends, ensure_ascii=False)
                    print("CASE " + json.dumps({"stream": stream, "timeout_only_master": True}), flush=True)
            finally:
                capture_task.cancel()
                await asyncio.gather(capture_task, return_exceptions=True)
        print("QQ_DUAL_TRANSPORT_PASS", flush=True)
    finally:
        if manager:
            for star in q.star_registry:
                if star.root_dir_name == "astrbot_plugin_shio":
                    await star.star_cls.terminate()
        await provider.client.close()
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)
        await server.cleanup()
        await q.db_helper.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
