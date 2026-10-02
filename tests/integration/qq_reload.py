"""Real Pipeline/PluginManager reloads; only HTTP model and QQ peer are fixtures."""
from qq_delivery import (
    asyncio, base64, copy, json, os, Path, web, db_helper, sp, settings,
    AiocqhttpAdapter, Endpoint, ProviderOpenAIOfficial, FixtureContext,
    PluginManager, star_registry, PipelineContext, PipelineScheduler,
    WakingCheckStage, ProcessStage, ResultDecorateStage, RespondStage,
    peer_connect, capture_sends, event_from_qq, text_bubbles,
)


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


async def main():
    await db_helper.initialize()
    await sp.initialize()
    config = settings()
    plugin_config = {"sys001": {
        "ingress": {"group_allowed_scopes": ["10001"], "private_allowed_sender_ids": ["101"]},
        "group": {"continuous_window_enabled": True, "continuous_window_seconds": 1,
                  "natural_participation_enabled": False},
        "lifecycle": {"shutdown_grace_seconds": 2},
        "master_alert": {"master_alert_enabled": True, "main_reply_exhausted_enabled": True,
                         "master_alert_quiet_enabled": False},
        "final_review": {"mode": "off"},
        "presentation": {"text_component_mode": "plugin", "text_component_min_segments": 1,
                         "text_component_max_segments": 3, "bubble_send_min_wait_seconds": 0,
                         "bubble_send_max_wait_seconds": 0},
    }}
    Path("data/config").mkdir(parents=True, exist_ok=True)
    Path("data/config/astrbot_plugin_shio_config.json").write_text(json.dumps(plugin_config))
    # Actual Meme hooks must finish as part of the same old Pipeline.
    image_dir = Path("data/plugin_data/meme_manager/packs/builtin-default/memes/happy")
    image_dir.mkdir(parents=True, exist_ok=True)
    image_dir.joinpath("fixture.png").write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/zg7WAAAAAElFTkSuQmCC"))
    Path("data/config/meme_manager_config.json").write_text(json.dumps({"generation": {"trigger": {"scope": "chat_and_plugin_llm"}, "emotion": {"llm": {"enabled": False}}, "message": {"enable_mixed": False}}, "semantic": {"enabled": False}}))
    queue = asyncio.Queue()
    adapter = AiocqhttpAdapter({"id": "fixture-qq", "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 18081}, config["platform_settings"], queue)
    bot_task = asyncio.create_task(adapter.bot.run_task("127.0.0.1", 18081))
    endpoint = Endpoint()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", endpoint.handle)
    server = web.AppRunner(app)
    await server.setup()
    await web.TCPSite(server, "127.0.0.1", 18082).start()
    tasks = []
    failures = []
    try:
        provider = ProviderOpenAIOfficial({"id": "fixture", "type": "openai_chat_completion", "key": ["fixture-key"], "model": "fixture", "api_base": "http://127.0.0.1:18082/v1", "max_context_tokens": 0}, config["provider_settings"])
        context = FixtureContext(config, provider, adapter, queue)
        manager = PluginManager(context, config)
        for name in ("meme_manager", "astrbot_plugin_shio", "astrbot"):
            loaded, error = await manager.load(specified_dir_name=name)
            assert loaded, error
        pipeline_context = PipelineContext(astrbot_config=config, plugin_manager=manager, astrbot_config_id="fixture")
        scheduler = PipelineScheduler(pipeline_context)
        scheduler.stages = [WakingCheckStage(), ProcessStage(), ResultDecorateStage(), RespondStage()]
        for stage in scheduler.stages:
            await stage.initialize(pipeline_context)
        async with await peer_connect() as ws:
            captures = []
            capture_task = asyncio.create_task(capture_sends(ws, captures))
            tasks.append(capture_task)
            for number, scenario in enumerate(("merge_reload", "model_reload", "bubble_reload", "model_disable", "timeout_master_only"), 801):
                shio = next(p.star_cls for p in star_registry if p.root_dir_name == "astrbot_plugin_shio")
                if scenario == "timeout_master_only":
                    master = await event_from_qq(adapter, private=True, number=800, text="绑定通知")
                    await scheduler.stages[0].process(master)
                    assert master.is_admin(), "Official waking stage must identify Master"
                    assert await shio.capture_master_alert_binding(master, shio._snapshot(master, "private"))
                    shio.config["sys001"]["lifecycle"] = {"shutdown_grace_seconds": 1}
                captures.clear()
                endpoint.requests.clear()
                endpoint.responses = [{"role": "assistant", "content": "听见啦。\n慢慢说。\n&&happy&&",
                                       "_fixture_delay": 3 if scenario == "timeout_master_only" else .25}]
                at_gap = asyncio.Event()
                release_gap = asyncio.Event()
                if scenario == "bubble_reload":
                    async def gap(*args):
                        at_gap.set()
                        await release_gap.wait()
                    shio._bubble_sleep = gap
                item = await event_from_qq(adapter, private=False, number=number, sender="101", text="在吗？")
                reply = asyncio.create_task(scheduler.execute(item))
                tasks.append(reply)
                if scenario == "merge_reload":
                    await until(lambda: bool(shio._batch_scopes))
                elif scenario == "bubble_reload":
                    await asyncio.wait_for(at_gap.wait(), 5)
                    asyncio.get_running_loop().call_later(.15, release_gap.set)
                else:
                    await until(lambda: bool(endpoint.requests))
                start = asyncio.get_running_loop().time()
                if scenario == "model_disable":
                    await asyncio.wait_for(manager.turn_off_plugin("astrbot_plugin_shio"), 10)
                else:
                    # Same public config save + real PluginManager.reload used by WebUI.
                    shio.config.save_config(copy.deepcopy(dict(shio.config)))
                    loaded, error = await asyncio.wait_for(manager.reload("astrbot_plugin_shio"), 10)
                    assert loaded, error
                elapsed = asyncio.get_running_loop().time() - start
                await asyncio.wait_for(asyncio.gather(reply, return_exceptions=True), 5)
                await asyncio.sleep(.1)
                group_sends = [p for p in captures if p.get("action") == "send_group_msg"]
                private_sends = [p for p in captures if p.get("action") == "send_private_msg"]
                images = sum(any(c.get("type") == "image" for c in p.get("params", {}).get("message", [])) for p in group_sends)
                if scenario == "timeout_master_only":
                    ok = not group_sends and len(private_sends) == 1 and str(private_sends[0]["params"]["user_id"]) == "100" and "等待" in "".join(text_bubbles(private_sends)) and elapsed < 5
                else:
                    ok = text_bubbles(group_sends) == ["听见啦。", "慢慢说。"] and images == 1 and not endpoint.responses and len(endpoint.requests) == 1
                print("CASE " + json.dumps({"scenario": scenario, "ok": ok, "bubbles": text_bubbles(group_sends), "images": images, "master_alerts": len(private_sends), "reload_seconds": round(elapsed, 3)}, ensure_ascii=False), flush=True)
                if not ok:
                    failures.append(scenario)
                if scenario == "model_disable":
                    await manager.turn_on_plugin("astrbot_plugin_shio")
            capture_task.cancel()
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)
        await server.cleanup()
        await db_helper.engine.dispose()
    assert not failures, f"Reload lost replies: {failures}"
    print("QQ_RELOAD_PASS", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
