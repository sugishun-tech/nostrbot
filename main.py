import gc
import json
import os
import random
import re
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone

import ollama
import requests
from pynostr.encrypted_dm import EncryptedDirectMessage
from pynostr.event import Event, EventKind
from pynostr.filters import Filters, FiltersList
from pynostr.key import PrivateKey
from pynostr.message_type import ClientMessageType
from pynostr.relay_manager import RelayManager
from pynostr.utils import get_timestamp

import prompts

relay_manager = RelayManager(timeout=2)
model = "hf.co/mmnga/Llama-3.1-Swallow-8B-Instruct-v0.5-gguf"


JST = timezone(timedelta(hours=+9))


def system_message():
    return prompts.system


def respond(message, theme=None, is_autonomous=False):
    if is_autonomous:
        prompt = prompts.prompt.format(theme=theme)
    else:
        # 返信用
        prompt = message
        print(prompt)
    messages = [
        {"role": "system", "content": system_message()},
        {"role": "user", "content": prompt}
    ]

    cs = prompts.cs.split(",")
    response = ollama.chat(model=model, messages=messages)['message']['content']
    for rmc in prompts.rmcs.split(","):
        response = response.replace(rmc, "")
    response = '。'.join([x for x in response.split("。") if all(c not in x for c in cs)]).strip()
    return response


def run():
    messages_done = []
    # 初回起動時に即投稿したい場合は、ここを 0 に設定してください
    last_post_time = 0
    POST_INTERVAL = 3600  # 60分

    env_private_key = os.environ.get("PRIVATE_KEY")
    if not env_private_key:
        print('PRIVATE_KEY is not set.')
        exit(1)

    private_key = PrivateKey.from_nsec(env_private_key)
    env_relays = os.getenv('RELAYS') or "wss://relay-jp.nostr.wirednet.jp/,wss://yabu.me/,wss://r.kojira.io/,wss://nrelay-jp.c-stellar.net/"

    for relay in env_relays.split(","):
        relay_manager.add_relay(relay)

    print(f"AI 稼働中... (Pubkey: {private_key.public_key.bech32()})")
    start_timestamp = get_timestamp()

    while (True):
        now = time.time()
        now_jst = datetime.now(JST)
        ch = now_jst.hour
        wd = now_jst.weekday()
        if wd == 4 and ch >= 15:
            time.sleep(3600)
            continue
        if wd == 5:
            time.sleep(3600)
            continue
        if ch >= 21 or ch < 6:
            print("sleeping...zzz")
            time.sleep(600)
            continue

        # --- 1. 自発的な定期投稿 ---
        if now - last_post_time >= POST_INTERVAL:
            print("定期投稿を送信中...")
            random.seed(time.time())
            themes = prompts.themes
            theme = random.choice([t.strip() for t in themes.split(",") if t.strip()])
            post_content = respond("", theme=theme, is_autonomous=True)
            note = Event(content=post_content, kind=EventKind.TEXT_NOTE)
            note.sign(private_key.hex())

            relay_manager.run_sync()
            relay_manager.publish_event(note)
            print(f"投稿完了: {post_content[:20]}...")
            last_post_time = now

        # --- 2. メンションへの反応 ---
        # 自分宛のメッセージ（pubkey_refs）のみをフィルタリング
        filters = FiltersList([
            Filters(pubkey_refs=[private_key.public_key.hex()],
                    kinds=[EventKind.TEXT_NOTE],
                    since=start_timestamp)
        ])

        subscription_id = uuid.uuid1().hex
        relay_manager.add_subscription_on_all_relays(subscription_id, filters)
        relay_manager.run_sync()

        while relay_manager.message_pool.has_events():
            event_msg = relay_manager.message_pool.get_event()

            if event_msg.event.id in messages_done:
                continue

            # 自分の投稿（定期投稿など）には反応しない
            if event_msg.event.pubkey == private_key.public_key.hex():
                continue

            messages_done.append(event_msg.event.id)
            recipient_pubkey = event_msg.event.pubkey

            # 公開メンション
            if event_msg.event.kind == EventKind.TEXT_NOTE:
                content = event_msg.event.content
                print(f"メンション受信: {content[:30]}")
                clean_content = re.sub(r'\b(nostr:)?(nprofile|npub)[0-9a-z]+[\s]*', '', content)
                reply = Event(content=respond(clean_content))
                reply.add_event_ref(event_msg.event.id)
                reply.add_pubkey_ref(event_msg.event.pubkey)
                reply.sign(private_key.hex())
                relay_manager.publish_event(reply)

            gc.collect()

        time.sleep(10)
        relay_manager.close_all_relay_connections()


try:
    run()
except KeyboardInterrupt:
    print("停止しました")
    relay_manager.close_all_relay_connections()
    exit(0)
except Exception as e:
    print(f"エラー再起動中: {e}")
    time.sleep(10)
    run()
