# Claude の、ユーザーに見える文が英語に崩れていないかを調べるフック。
# 使い方: lang_check.py stop | tool （標準入力にフックの JSON）
# - stop: 応答の最後の文が英語なら、差し戻して日本語で書き直させる（同じ応答では 1 回だけ）。
# - tool: ツールを呼んだ後、直前の途中経過の文が英語なら、Claude に注意を返す（作業は止めない）。
# 英語かどうか: コードブロック・バッククォートの中・URL・パスを除き、英字 ÷（英字 + 日本語の文字）が
# 0.8 を超え、数えた文字が 40 以上のとき。
import json
import os
import re
import sys

THRESHOLD = 0.8
MIN_CHARS = 40
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")

FENCE = re.compile(r"```.*?(```|$)", re.S)
INLINE = re.compile(r"`[^`\n]*`")
URL = re.compile(r"https?://\S+|claude://\S+")
LINK_TARGET = re.compile(r"\]\([^)]*\)")
PATH = re.compile(r"(?:[A-Za-z]:[\\/]|[\\/]{1,2}|~[\\/])[^\s、。）)」]*|\b[\w.-]+[\\/][\w.\\/-]+")
JP = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")
EN = re.compile(r"[A-Za-z]")


def english_ratio(text):
    """（英字の割合, 数えた文字数）。コード・URL・パスは数えない。"""
    t = FENCE.sub(" ", text)
    t = INLINE.sub(" ", t)
    t = LINK_TARGET.sub("]", t)
    t = URL.sub(" ", t)
    t = PATH.sub(" ", t)
    en, jp = len(EN.findall(t)), len(JP.findall(t))
    total = en + jp
    return (en / total if total else 0.0), total


def texts(transcript):
    """会話記録の、Claude の文のエントリ（uuid, 文）を順に。"""
    out = []
    try:
        with open(transcript, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                content = d.get("message", {}).get("content")
                if not isinstance(content, list):
                    continue
                body = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
                if body.strip():
                    out.append((d.get("uuid", ""), body))
    except OSError:
        pass
    return out


def load_state(session):
    """調べ終えたエントリの uuid。初めてのセッションでは None。"""
    try:
        with open(os.path.join(STATE_DIR, f"{session}.json"), encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return None


def save_state(session, uuids):
    """調べ終えたエントリの uuid を、会話記録の順で最新の 2000 件まで保存する。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"{session}.json"), "w", encoding="utf-8") as f:
        json.dump(uuids[-2000:], f)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "tool"
    for stream in (sys.stdin, sys.stdout):  # 標準入出力が cp932 の環境でも、フックの JSON を UTF-8 で読み書きする
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    data = json.load(sys.stdin)
    transcript = data.get("transcript_path", "")
    session = re.sub(r"[^\w-]", "_", data.get("session_id", "unknown"))
    seen = load_state(session)
    entries = texts(transcript)
    if seen is None:  # 途中から入れたセッション: それまでの文は調べず、最新の 1 つだけを見る
        seen = {u for u, _ in entries[:-1]}
    new = [(u, t) for u, t in entries if u not in seen]
    bad = []
    for u, t in new:
        ratio, total = english_ratio(t)
        if total >= MIN_CHARS and ratio > THRESHOLD:
            bad.append((ratio, t))
        seen.add(u)
    save_state(session, [u for u, _ in entries if u in seen])  # 会話記録の順で保存する（uuid の文字列順にしない）
    if not bad:
        return
    ratio, sample = bad[-1]
    head = sample.strip().splitlines()[0][:80]
    msg = (f"ユーザーに見える文が英語になっています（英字の割合 {ratio:.0%}。冒頭: 「{head}」）。"
           "このプロジェクトでは、途中経過も含めてすべて日本語（です・ます調）で書く決まりです。")
    if mode == "stop":
        if data.get("stop_hook_active"):
            return  # 差し戻しは 1 回だけ（止まらなくなるのを防ぐ）
        print(json.dumps({"decision": "block",
                          "reason": msg + "直前の応答を、内容を変えずに日本語で書き直してください。"},
                         ensure_ascii=False))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": msg + "次の文から日本語で書いてください。"}},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
