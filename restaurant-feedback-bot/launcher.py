from __future__ import annotations

import asyncio
import os

# bot.main() normally starts the Mini App server itself. The launcher owns the
# single Railway PORT so we disable that internal start and build the same app
# with an extra middleware for platform auth + Master Audit.
os.environ["MINIAPP_HTTP"] = "0"

from aiohttp import web

import bot
import menu_training
import menu_training_nudges
import menu_training_publish
import menu_training_review
import miniapp_api
import platform_api

# Publication and review handlers are registered before the core training module
# so shared callbacks use the privacy-safe version-aware implementations.
menu_training_publish.configure(
    bot.load_data,
    bot.save_data,
    bot.is_global_admin,
)
menu_training_publish.register(bot.dp)

menu_training_review.configure(
    bot.load_data,
    bot.save_data,
    bot.is_global_admin,
)
menu_training_review.register(bot.dp)

menu_training.configure(
    bot.bot,
    bot.load_data,
    bot.save_data,
    bot.is_global_admin,
)
menu_training.register(bot.dp)

menu_training_nudges.configure(bot.bot, bot.load_data)
menu_training_nudges.register(bot.dp)


async def start_http() -> None:
    me = await bot.bot.get_me()
    app = miniapp_api.make_aiohttp_app(
        bot_token=bot.TOKEN,
        load_data=bot.load_data,
        is_global_admin_fn=bot.is_global_admin,
        bot_username=me.username or "",
        jsonl_path=bot.FEEDBACK_LOG_PATH,
        save_data=bot.save_data,
        resolve_username=bot._resolve_telegram_username,
    )
    # This middleware intercepts /api/platform/* before the existing static '/'
    # handler, so the old Telegram Mini App keeps working unchanged.
    app.middlewares.insert(0, platform_api.make_middleware())

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("MINIAPP_PORT", os.getenv("PORT", "8080")))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[http] combined miniapp + platform API on 0.0.0.0:{port}")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await platform_api.close_pool()


async def main() -> None:
    await asyncio.gather(
        bot.main(),
        start_http(),
        menu_training.background_worker(),
        menu_training_nudges.worker(),
    )


if __name__ == "__main__":
    asyncio.run(main())
