import gc
import logging
import os
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

# ログ設定
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class NostrBot:
    def __init__(self):
        self.jst = timezone(timedelta(hours=+9))
        self.model = "hf.co/mmnga/Llama-3.1-Swallow-8B-Instruct-v0.5-gguf"
        self.post_interval = 3600  # 60分
        self.last_post_time = 0
        self.messages_done = set()

        # 環境変数チェック
        env_private_key = os.environ.get("PRIVATE_KEY")
        if not env_private_key:
            raise ValueError("PRIVATE_KEY is not set.")

        self.private_key = PrivateKey.from_nsec(env_private_key)
        self.pubkey_hex = self.private_key.public_key.hex()

        relays = os.getenv('RELAYS', "wss://relay-jp.nostr.wirednet.jp/,wss://yabu.me/,wss://r.kojira.io/,wss://nrelay-jp.c-stellar.net/")
        self.relay_urls = [r.strip() for r in relays.split(",") if r.strip()]

        self.relay_manager = RelayManager(timeout=2)

    def get_system_message(self):
        """現在の時刻を含めたシステムプロンプトを生成"""
        now_str = datetime.now(self.jst).strftime("%I %p %A")
        return prompts.system.format(cds=now_str)

    def generate_response(self, content, theme=None, is_autonomous=False):
        """AIによる応答生成とフィルタリング"""
        prompt_text = prompts.prompt.format(theme=theme) if is_autonomous else content

        messages = [
            {"role": "system", "content": self.get_system_message()},
            {"role": "user", "content": prompt_text}
        ]

        try:
            response = ollama.chat(model=self.model, messages=messages)['message']['content']

            # クリーニング処理
            for rmc in prompts.rmcs.split(","):
                response = response.replace(rmc, "")

            # 禁止ワードを含む一文を除去
            cs = prompts.cs.split(",")
            sentences = response.split("。")
            filtered_sentences = [s for s in sentences if all(c not in s for c in cs)]

            return "。".join(filtered_sentences).strip()
        except Exception as e:
            logger.error(f"AI生成エラー: {e}")
            return "..."

    def is_sleep_time(self):
        """稼働時間外（夜間や特定の曜日）か判定"""
        now = datetime.now(self.jst)
        hour = now.hour
        weekday = now.weekday()

        # 金曜15時以降〜土曜終日は休み
        if (weekday == 4 and hour >= 15) or (weekday == 5):
            return True
        # 夜間（21時〜翌6時）は休み
        if hour >= 21 or hour < 6:
            return True
        return False

    def setup_relays(self):
        """リレーの初期化と購読設定"""
        self.relay_manager.close_all_relay_connections()  # 一旦リセット
        for url in self.relay_urls:
            self.relay_manager.add_relay(url)

        filters = FiltersList([
            Filters(pubkey_refs=[self.pubkey_hex],
                    kinds=[EventKind.TEXT_NOTE],
                    since=get_timestamp())
        ])
        subscription_id = uuid.uuid1().hex
        self.relay_manager.add_subscription_on_all_relays(subscription_id, filters)
        logger.info(f"ボット稼働開始 (Pubkey: {self.private_key.public_key.bech32()})")

    def start(self):
        """メインループ"""
        self.setup_relays()

        while True:
            try:
                # 1. スリープ判定
                if self.is_sleep_time():
                    logger.info("稼働時間外のため待機中...")
                    time.sleep(600)
                    continue

                # 2. 定期投稿
                now_ts = time.time()
                if now_ts - self.last_post_time >= self.post_interval:
                    import random
                    theme = random.choice([t.strip() for t in prompts.themes.split(",") if t.strip()])
                    content = self.generate_response("", theme=theme, is_autonomous=True)

                    if content:
                        note = Event(content=content, kind=EventKind.TEXT_NOTE)
                        note.sign(self.private_key.hex())
                        self.relay_manager.publish_event(note)
                        logger.info(f"定期投稿完了: {content[:20]}...")

                    self.last_post_time = now_ts

                # 3. メンション反応
                self.relay_manager.run_sync()
                while self.relay_manager.message_pool.has_events():
                    event_msg = self.relay_manager.message_pool.get_event()
                    event = event_msg.event

                    if event.id in self.messages_done or event.pubkey == self.pubkey_hex:
                        continue

                    self.messages_done.add(event.id)

                    # 公開メンションへの返信
                    if event.kind == EventKind.TEXT_NOTE:
                        # npub等の除去
                        clean_content = re.sub(r'\b(nostr:)?(nprofile|npub)[0-9a-z]+[\s]*', '', event.content)
                        logger.info(f"メンション受信: {clean_content[:30]}")

                        reply_text = self.generate_response(clean_content)
                        if reply_text:
                            reply = Event(content=reply_text)
                            reply.add_event_ref(event.id)
                            reply.add_pubkey_ref(event.pubkey)
                            reply.sign(self.private_key.hex())
                            self.relay_manager.publish_event(reply)

                # 4. 少し待機してリソース節約
                time.sleep(5)
                gc.collect()

            except Exception as e:
                logger.error(f"ループ内でエラー発生: {e}")
                time.sleep(10)
                # 接続エラーの可能性もあるためリレーを再セットアップ
                try:
                    self.setup_relays()
                except:
                    pass


if __name__ == "__main__":
    bot = NostrBot()
    try:
        bot.start()
    except KeyboardInterrupt:
        logger.info("停止しました")
        bot.relay_manager.close_all_relay_connections()
