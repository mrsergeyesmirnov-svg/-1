"""Runtime compatibility shim for Railway deployments that still start ``python bot.py``.

Python imports sitecustomize automatically on startup. We use that hook only when the
entrypoint is bot.py, so the newer menu-training handlers/UI cleanup are installed even
if Railway has an old explicit Start Command overriding Procfile/railway.json.

When launcher.py is used this file intentionally does nothing because launcher already
installs the same modules itself.
"""
from __future__ import annotations

import asyncio
import os
import sys


def _running_legacy_bot_entrypoint() -> bool:
    try:
        return os.path.basename(sys.argv[0] or "") == "bot.py"
    except Exception:
        return False


if _running_legacy_bot_entrypoint():
    try:
        from aiogram import Dispatcher

        _original_start_polling = Dispatcher.start_polling

        async def _start_polling_with_menu_training(self, *bots, **kwargs):
            if not getattr(self, "_pulse_menu_training_installed", False):
                # At this point bot.py has finished defining load_data/save_data/access
                # helpers and is entering polling, so __main__ is safe to configure.
                bot_module = sys.modules.get("__main__")
                if bot_module is None:
                    raise RuntimeError("bot runtime module not found")

                import menu_training
                import menu_training_hotfix
                import menu_training_nudges
                import menu_training_publish
                import menu_training_review

                # Clean manager-facing Materials UI first.
                menu_training_hotfix.apply_ui_cleanup(bot_module, menu_training_review)
                menu_training_hotfix.configure(
                    bot_module.bot,
                    bot_module.load_data,
                    bot_module.save_data,
                    bot_module.is_global_admin,
                )
                # Register immediate analysis before the older queue-only callbacks.
                menu_training_hotfix.register(self)

                menu_training_publish.configure(
                    bot_module.load_data,
                    bot_module.save_data,
                    bot_module.is_global_admin,
                )
                menu_training_publish.register(self)

                menu_training_review.configure(
                    bot_module.load_data,
                    bot_module.save_data,
                    bot_module.is_global_admin,
                )
                menu_training_review.register(self)

                menu_training.configure(
                    bot_module.bot,
                    bot_module.load_data,
                    bot_module.save_data,
                    bot_module.is_global_admin,
                )
                menu_training.register(self)

                menu_training_nudges.configure(bot_module.bot, bot_module.load_data)
                menu_training_nudges.register(self)

                # Keep automatic processing/reminders alive in legacy bot.py mode.
                asyncio.create_task(menu_training.background_worker())
                asyncio.create_task(menu_training_nudges.worker())

                setattr(self, "_pulse_menu_training_installed", True)
                print("[menu-training] legacy bot.py runtime patched")

            return await _original_start_polling(self, *bots, **kwargs)

        Dispatcher.start_polling = _start_polling_with_menu_training
        print("[sitecustomize] bot.py compatibility hook enabled")
    except Exception as exc:
        # Never prevent the core bot from starting if the compatibility hook fails.
        print(f"[sitecustomize] compatibility hook failed: {exc!r}")
