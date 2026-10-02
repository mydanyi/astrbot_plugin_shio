"""Integration test: Shio QQ task retirement on no-model drain and reload.

Synthetic data only. The official agent is exercised as-is (not stubbed or
monkeypatched), and no direct calls are made into Shio's terminal cleanup.
"""

from qq_delivery import (
    asyncio,
    json,
    Path,
    web,
    db_helper,
    sp,
    settings,
    AiocqhttpAdapter,
    Endpoint,
    ProviderOpenAIOfficial,
    FixtureContext,
    PluginManager,
    star_registry,
    PipelineContext,
    PipelineScheduler,
    WakingCheckStage,
    ProcessStage,
    ResultDecorateStage,
    RespondStage,
    peer_connect,
    capture_sends,
    event_from_qq,
    text_bubbles,
)


SEND_ACTIONS = ("send_group_msg", "send_private_msg", "send_msg")


def _write_synthetic_plugin_config():
    plugin_config = {
        "sys001": {
            "ingress": {
                "group_allowed_scopes": ["10001"],
                "private_allowed_sender_ids": ["101"],
            },
            "group": {
                "continuous_window_enabled": False,
                "natural_participation_enabled": False,
            },
            "lifecycle": {
                "shutdown_grace_seconds": 2,
            },
            "final_review": {
                "mode": "off",
            },
            "presentation": {
                "text_component_mode": "plugin",
                "text_component_min_segments": 1,
                "text_component_max_segments": 3,
                "bubble_send_min_wait_seconds": 0,
                "bubble_send_max_wait_seconds": 0,
            },
        }
    }
    config_dir = Path("data/config")
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "astrbot_plugin_shio_config.json"
    config_path.write_text(
        json.dumps(plugin_config, ensure_ascii=False), encoding="utf-8"
    )


def _make_event(adapter, index):
    number = 9100 + index
    text = "无模型收尾测试"
    if index % 3 == 0:
        return event_from_qq(
            adapter, private=False, number=number, sender="101",
            text=text, mention=True, reply=False,
        )
    if index % 3 == 1:
        return event_from_qq(
            adapter, private=True, number=number, sender="101",
            text=text, mention=False, reply=False,
        )
    return event_from_qq(
        adapter, private=False, number=number, sender="101",
        text=text, mention=False, reply=True,
    )


async def _initialize_stages(stages, pipeline_context):
    for stage in stages:
        await stage.initialize(pipeline_context)


async def main():
    owned_tasks = []
    runner = None
    try:
        await db_helper.initialize()
        await sp.initialize()

        config = settings()
        config["provider_settings"]["enable"] = False

        _write_synthetic_plugin_config()

        queue = asyncio.Queue()
        adapter = AiocqhttpAdapter(
            {
                "id": "fixture-qq",
                "ws_reverse_host": "127.0.0.1",
                "ws_reverse_port": 18081,
            },
            config["platform_settings"],
            queue,
        )
        bot_task = asyncio.create_task(adapter.bot.run_task("127.0.0.1", 18081))
        owned_tasks.append(bot_task)

        endpoint = Endpoint()
        app = web.Application()
        app.router.add_post("/v1/chat/completions", endpoint.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 18082)
        await site.start()

        provider = ProviderOpenAIOfficial(
            {
                "id": "fixture",
                "type": "openai_chat_completion",
                "key": ["fixture-key"],
                "model": "fixture",
                "api_base": "http://127.0.0.1:18082/v1",
                "max_context_tokens": 0,
            },
            config["provider_settings"],
        )

        context = FixtureContext(config, provider, adapter, queue)

        manager = PluginManager(context, config)
        for name in ("astrbot_plugin_shio", "astrbot"):
            loaded, error = await manager.load(specified_dir_name=name)
            assert loaded, error

        shio = next(
            plugin.star_cls
            for plugin in star_registry
            if plugin.root_dir_name == "astrbot_plugin_shio"
        )

        pipeline_context = PipelineContext(
            astrbot_config=config,
            plugin_manager=manager,
            astrbot_config_id="fixture",
        )
        scheduler = PipelineScheduler(pipeline_context)
        stages = (
            WakingCheckStage(),
            ProcessStage(),
            ResultDecorateStage(),
            RespondStage(),
        )
        scheduler.stages = list(stages)
        await _initialize_stages(stages, pipeline_context)

        captures = []
        async with await peer_connect() as ws:
            capture_task = asyncio.create_task(capture_sends(ws, captures))
            owned_tasks.append(capture_task)

            tasks = []
            for index in range(40):
                event = await _make_event(adapter, index)
                task = asyncio.create_task(scheduler.execute(event))
                tasks.append(task)
                owned_tasks.append(task)

            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            await asyncio.sleep(0.01)

            assert all(task.done() for task in tasks)
            assert not shio._batch_scopes
            assert not shio._reply_drain._tasks
            assert not endpoint.requests
            assert text_bubbles(captures) == []
            assert not [
                capture
                for capture in captures
                if capture.get("action") in SEND_ACTIONS
            ]

            print("CASE no_model tasks=40 completed=40 no_orphans=True")

            config["provider_settings"]["enable"] = True
            await _initialize_stages(stages, pipeline_context)

            endpoint.responses = [{"role": "assistant", "content": "收到啦。"}]

            event = await event_from_qq(
                adapter, private=False, number=9200, sender="101",
                text="无模型收尾测试", mention=True, reply=False,
            )
            task = asyncio.create_task(scheduler.execute(event))
            owned_tasks.append(task)
            await asyncio.wait_for(task, 10)
            await asyncio.sleep(0.01)

            assert len(endpoint.requests) == 1
            assert text_bubbles(captures) == ["收到啦。"]
            assert not shio._batch_scopes
            assert not shio._reply_drain._tasks

            loop = asyncio.get_event_loop()
            start = loop.time()
            loaded, error = await asyncio.wait_for(
                manager.reload("astrbot_plugin_shio"), 8
            )
            reload_seconds = loop.time() - start
            assert loaded, error
            assert reload_seconds <= 3

            print(f"CASE restored reload_seconds={reload_seconds:.2f}")
            print("QQ_TASK_RETIREMENT_PASS")
    finally:
        for task in owned_tasks:
            if task is not None and not task.done():
                task.cancel()
        if owned_tasks:
            await asyncio.gather(*owned_tasks, return_exceptions=True)
        if runner is not None:
            await runner.cleanup()
        engine = getattr(db_helper, "engine", None)
        if engine is not None:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
