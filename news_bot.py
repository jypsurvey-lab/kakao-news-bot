"""
카카오톡 아침 뉴스 봇 (전체보기 링크 방식)
- RSS 수집 → Gemini(무료)로 풍성한 브리핑 생성 → 저장소에 briefings/latest.md 저장
- 카카오톡에는 핵심 헤드라인 요약 1개 + "전체 브리핑 보기" 링크 버튼 전송

필요 GitHub Secrets:
  KAKAO_REST_API_KEY   카카오 앱의 REST API 키
  KAKAO_REFRESH_TOKEN  최초 1회 발급받은 refresh token
  GEMINI_API_KEY       Google AI Studio에서 무료 발급한 Gemini API 키
  (선택) GH_PAT        refresh token 자동 갱신용 GitHub PAT (repo scope)
GH_REPO는 워크플로우가 자동으로 넣어줍니다.
"""

import os
import sys
import json
import base64
import time
import datetime
import xml.etree.ElementTree as ET
from urllib import request, parse, error

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────

RSS_FEEDS = {
    "종합": "https://www.yna.co.kr/rss/news.xml",           # 연합뉴스 최신
    "경제": "https://www.yna.co.kr/rss/economy.xml",         # 연합뉴스 경제
    "국제": "https://www.yna.co.kr/rss/international.xml",   # 연합뉴스 국제
}

MAX_ARTICLES_PER_FEED = 15     # 피드당 가져올 기사 수
# 무료 등급 모델 목록. 앞의 모델이 과부하/오류면 다음 모델로 자동 전환
GEMINI_MODELS = [
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
]
BRIEFING_PATH = "briefings/latest.md"   # 저장소에 저장될 전체 브리핑 파일
KST = datetime.timezone(datetime.timedelta(hours=9))


# ─────────────────────────────────────────────
# 1. RSS 뉴스 수집
# ─────────────────────────────────────────────

def fetch_rss(url: str, limit: int) -> list[dict]:
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0 (news-bot)"})
    with request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title and link:
            items.append({"title": title, "link": link, "desc": desc[:200], "pubDate": pub})
        if len(items) >= limit:
            break
    return items


def collect_news() -> str:
    blocks = []
    for category, url in RSS_FEEDS.items():
        try:
            articles = fetch_rss(url, MAX_ARTICLES_PER_FEED)
        except Exception as e:
            print(f"[warn] RSS 수집 실패 ({category}): {e}", file=sys.stderr)
            continue
        lines = [f"### 카테고리: {category}"]
        for a in articles:
            lines.append(f"- 제목: {a['title']}\n  요약: {a['desc']}\n  링크: {a['link']}\n  발행: {a['pubDate']}")
        blocks.append("\n".join(lines))
    if not blocks:
        raise RuntimeError("모든 RSS 피드 수집에 실패했습니다.")
    return "\n\n".join(blocks)


# ─────────────────────────────────────────────
# 2. Gemini API 호출 (재시도 + 모델 폴백)
# ─────────────────────────────────────────────

def call_gemini(system_prompt: str, user_prompt: str, max_tokens: int = 8000) -> str:
    api_key = os.environ["GEMINI_API_KEY"]
    body = json.dumps({
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }).encode()

    last_err = None
    for model in GEMINI_MODELS:
        for attempt in range(1, 4):
            try:
                req = request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    data=body,
                    headers={
                        "x-goog-api-key": api_key,
                        "content-type": "application/json",
                    },
                    method="POST",
                )
                with request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read())
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
                print(f"[info] Gemini 호출 성공 (모델: {model})")
                return text
            except error.HTTPError as e:
                last_err = e
                if e.code == 404:
                    print(f"[warn] {model} 사용 불가(404) — 다음 모델로 전환")
                    break
                if e.code in (429, 500, 502, 503):
                    wait = 20 * attempt
                    print(f"[warn] {model} HTTP {e.code} — {wait}초 후 재시도 ({attempt}/3)")
                    time.sleep(wait)
                    continue
                raise
            except (TimeoutError, OSError, error.URLError) as e:
                last_err = e
                wait = 20 * attempt
                print(f"[warn] {model} 통신 오류({e}) — {wait}초 후 재시도 ({attempt}/3)")
                time.sleep(wait)
                continue
        else:
            print(f"[warn] {model} 재시도 소진 — 다음 모델로 전환")
    raise RuntimeError(f"모든 Gemini 모델 시도 실패. 마지막 오류: {last_err}")


FULL_BRIEFING_PROMPT = """당신은 매일 아침 사용자에게 지난 하루의 주요 뉴스를 정리해 주는 개인 뉴스 큐레이터입니다.
제공된 뉴스 기사 목록으로 마크다운 형식의 아침 뉴스 브리핑을 작성하세요.

[규칙]
1. 실제 기사가 있는 카테고리만 포함하며 카테고리는 "## 📌 종합", "## 💰 경제", "## 🌍 국제" 형식의 제목으로 구분.
2. 카테고리별로 중요한 기사 5건 선정(기사가 부족하면 있는 만큼). 각 기사는 "**헤드라인**" 한 줄 + 핵심 내용 2~3문장 요약 + 기사 원문 링크 한 줄로 구성.
3. 중복되거나 사실상 같은 사건을 다루는 기사는 하나로 합침.
4. 자극적이거나 클릭베이트성 표현 금지. 사실 위주로 담백하게.
5. 절대 금지: 글자 수 표기, "(90 chars)" 같은 메타 주석, 작성 과정 설명, 인사말. 브리핑 본문만 출력.
6. 맨 마지막 줄에 "---" 아래 "연합뉴스 RSS 기반 · 자동 생성 브리핑" 표기."""

DIGEST_PROMPT = """아래 뉴스 브리핑에서 가장 중요한 서로 다른 헤드라인 3개를 뽑으세요.

[규칙]
1. 한 줄에 헤드라인 하나씩, 정확히 3줄만 출력.
2. 각 헤드라인은 20자 이내로 압축.
3. 기호(·, -, 숫자), 마크다운, 이모지, 설명, 글자 수 표기 일절 금지. 헤드라인 텍스트만."""


def build_digest(raw: str) -> str:
    """Gemini가 뽑은 헤드라인을 코드가 직접 조립해 길이를 보장한다."""
    lines = []
    for line in raw.splitlines():
        clean = line.strip().lstrip("·-*0123456789. ").strip()
        if clean:
            lines.append(clean[:22])  # 헤드라인당 최대 22자로 강제
        if len(lines) == 3:
            break
    if not lines:
        lines = ["오늘의 주요 뉴스가 도착했습니다"]
    return "☀️ 오늘의 뉴스\n" + "\n".join(f"· {l}" for l in lines)


# ─────────────────────────────────────────────
# 3. 브리핑 파일 저장
# ─────────────────────────────────────────────

def save_briefing(briefing_md: str, now_kst: datetime.datetime):
    os.makedirs(os.path.dirname(BRIEFING_PATH), exist_ok=True)
    header = f"# ☀️ 아침 뉴스 브리핑 — {now_kst.strftime('%Y년 %m월 %d일')}\n\n"
    with open(BRIEFING_PATH, "w", encoding="utf-8") as f:
        f.write(header + briefing_md + "\n")
    print(f"[info] 브리핑 저장 완료: {BRIEFING_PATH}")


def briefing_url() -> str:
    repo = os.environ.get("GH_REPO", "")
    if repo:
        return f"https://github.com/{repo}/blob/main/{BRIEFING_PATH}"
    return "https://news.naver.com"


# ─────────────────────────────────────────────
# 4. 카카오 토큰 갱신 + 메시지 전송 (단일 메시지)
# ─────────────────────────────────────────────

def refresh_kakao_token() -> tuple[str, str | None]:
    payload = parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": os.environ["KAKAO_REST_API_KEY"],
        "refresh_token": os.environ["KAKAO_REFRESH_TOKEN"],
    }).encode()
    req = request.Request("https://kauth.kakao.com/oauth/token", data=payload, method="POST")
    with request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data["access_token"], data.get("refresh_token")


def send_kakao_memo(access_token: str, text: str, link_url: str):
    # 안전장치: 카카오 제한(200자)을 넘지 않도록 자름
    if len(text) > 190:
        text = text[:187] + "..."
    template = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": link_url, "mobile_web_url": link_url},
        "button_title": "전체 브리핑 보기",
    }
    payload = parse.urlencode({"template_object": json.dumps(template, ensure_ascii=False)}).encode()
    req = request.Request(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        data=payload,
        headers={"Authorization": f"Bearer {access_token}"},
        method="POST",
    )
    with request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        if result.get("result_code") != 0:
            raise RuntimeError(f"카카오 전송 실패: {result}")


# ─────────────────────────────────────────────
# 5. (선택) 새 refresh token을 GitHub Secret에 저장
# ─────────────────────────────────────────────

def update_github_secret(new_refresh_token: str):
    pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GH_REPO")
    if not pat or not repo:
        print("[warn] 새 refresh token 발급됨. GH_PAT/GH_REPO 미설정으로 자동 저장 불가.")
        print("[warn] KAKAO_REFRESH_TOKEN 시크릿을 수동으로 갱신하세요.")
        return
    try:
        from nacl import encoding, public  # type: ignore
    except ImportError:
        print("[warn] pynacl 미설치. 시크릿 자동 갱신 건너뜀.")
        return

    def gh_api(path, method="GET", body=None):
        req = request.Request(
            f"https://api.github.com{path}",
            data=json.dumps(body).encode() if body else None,
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
            },
            method=method,
        )
        with request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    key_info = gh_api(f"/repos/{repo}/actions/secrets/public-key")
    pk = public.PublicKey(key_info["key"].encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(new_refresh_token.encode())
    gh_api(
        f"/repos/{repo}/actions/secrets/KAKAO_REFRESH_TOKEN",
        method="PUT",
        body={
            "encrypted_value": base64.b64encode(sealed).decode(),
            "key_id": key_info["key_id"],
        },
    )
    print("[info] KAKAO_REFRESH_TOKEN 시크릿 자동 갱신 완료.")


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def main():
    now_kst = datetime.datetime.now(KST)
    print(f"[info] 실행 시각(KST): {now_kst.isoformat()}")

    print("[info] 뉴스 수집 중...")
    news_text = collect_news()

    print("[info] 전체 브리핑 생성 중...")
    briefing = call_gemini(
        FULL_BRIEFING_PROMPT,
        f"오늘 날짜: {now_kst.strftime('%m월 %d일')}\n\n[입력 뉴스 데이터]\n{news_text}",
    )
    save_briefing(briefing, now_kst)

    print("[info] 카톡용 요약 생성 중...")
    digest = build_digest(call_gemini(DIGEST_PROMPT, briefing))

    print("[info] 카카오 토큰 갱신 중...")
    access_token, new_refresh = refresh_kakao_token()

    print("[info] 카카오톡 전송 중...")
    send_kakao_memo(access_token, digest, briefing_url())
    print("[info] 전송 완료 ✅")

    if new_refresh:
        update_github_secret(new_refresh)


if __name__ == "__main__":
    try:
        main()
    except error.HTTPError as e:
        print(f"[error] HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
