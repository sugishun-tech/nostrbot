import gc
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import ollama
from pynostr.event import Event, EventKind
from pynostr.filters import Filters, FiltersList
from pynostr.key import PrivateKey
from pynostr.relay_manager import RelayManager
from pynostr.utils import get_timestamp

import prompts

# --- ログ設定：警告(WARNING)以上のみ表示（INFOレベルのメッセージは一切出ません） ---
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class NostrBot:
    def __init__(self):
        self.jst = timezone(timedelta(hours=+9))
        self.model = "hf.co/mmnga/Llama-3.1-Swallow-8B-Instruct-v0.5-gguf"
        self.post_interval = 3600
        self.last_post_time = 0
        self.messages_done = set()

        # 環境変数
        pk_env = os.environ.get("PRIVATE_KEY")
        if not pk_env:
            raise ValueError("PRIVATE_KEY is not set.")
        self.private_key = PrivateKey.from_nsec(pk_env)
        self.pubkey_hex = self.private_key.public_key.hex()

        relays = os.getenv('RELAYS', "wss://relay-jp.nostr.wirednet.jp/,wss://yabu.me/,wss://r.kojira.io/,wss://nrelay-jp.c-stellar.net/")
        self.relay_urls = [r.strip() for r in relays.split(",") if r.strip()]

    def generate_response(self, content, theme=None, is_autonomous=False):
        """AI応答生成（プロンプトクリーニング含む）"""
        now_str = datetime.now(self.jst).strftime("%I %p %A")
        sys_msg = prompts.system.format(cds=now_str)
        prompt_text = prompts.prompt.format(theme=theme) if is_autonomous else content

        messages = [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": prompt_text}
        ]

        try:
            res = ollama.chat(model=self.model, messages=messages)['message']['content']
            # 置換処理
            for rmc in prompts.rmcs.split(","):
                res = res.replace(rmc, "")
            # 禁止ワードフィルタ
            cs = prompts.cs.split(",")
            sentences = res.split("。")
            filtered = [s for s in sentences if all(c not in s for c in cs)]
            return "。".join(filtered).strip()
        except:
            return ""

    def is_sleep_time(self):
        """スリープ判定"""
        now = datetime.now(self.jst)
        if (now.weekday() == 4 and now.hour >= 15) or (now.weekday() == 5):
            return True
        if now.hour >= 21 or now.hour < 6:
            return True
        return False

    def run_cycle(self):
        """1回分の接続・投稿・受信サイクル"""
        # 毎回リレーマネージャーを新規作成（タイムアウト対策：接続を使いまわさない）
        manager = RelayManager(timeout=10)  # 余裕を持って10秒

        try:
            for url in self.relay_urls:
                manager.add_relay(url)

            # 購読設定
            sub_id = uuid.uuid1().hex
            filters = FiltersList([
                Filters(pubkey_refs=[self.pubkey_hex], kinds=[EventKind.TEXT_NOTE], since=get_timestamp())
            ])
            manager.add_subscription_on_all_relays(sub_id, filters)

            # 接続実行（ここでタイムアウトしても except でキャッチして次へ行く）
            manager.run_sync()

            # --- 定期投稿 ---
            now_ts = time.time()
            if now_ts - self.last_post_time >= self.post_interval:
                # 【重要】乱数シードを現在時刻で固定
                random.seed(time.time())
                themes = [t.strip() for t in prompts.themes.split(",") if t.strip()]
                if themes:
                    theme = random.choice(themes)
                    content = self.generate_response("", theme=theme, is_autonomous=True)
                    if content:
                        note = Event(content=content, kind=EventKind.TEXT_NOTE)
                        note.sign(self.private_key.hex())
                        manager.publish_event(note)
                        self.last_post_time = now_ts

            # --- リプライ反応 ---
            while manager.message_pool.has_events():
                event_msg = manager.message_pool.get_event()
                ev = event_msg.event
                if ev.id in self.messages_done or ev.pubkey == self.pubkey_hex:
                    continue

                self.messages_done.add(ev.id)
                # メンション除去
                clean_content = re.sub(r'\b(nostr:)?(nprofile|npub)[0-9a-z]+[\s]*', '', ev.content)
                reply_text = self.generate_response(clean_content)

                if reply_text:
                    reply = Event(content=reply_text)
                    reply.add_event_ref(ev.id)
                    reply.add_pubkey_ref(ev.pubkey)
                    reply.sign(self.private_key.hex())
                    manager.publish_event(reply)

        except Exception as e:
            # タイムアウト等が発生してもログだけ残して止まらない
            logger.warning(f"Cycle Error (will retry): {e}")
        finally:
            # 必ず接続を閉じてリソースを解放
            manager.close_all_relay_connections()
            gc.collect()

    def start(self):
        """メインループ"""
        while True:
            if self.is_sleep_time():
                time.sleep(600)
                continue

            self.run_cycle()
            time.sleep(10)  # 次のサイクルまで待機


if __name__ == "__main__":
    bot = NostrBot()
    try:
        bot.start()
    except KeyboardInterrupt:
        pass
