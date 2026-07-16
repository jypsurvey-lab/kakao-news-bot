"""
카카오톡 뉴스 브리핑 봇
- RSS로 뉴스 수집 → Gemini API(무료)로 요약 → 카카오 "나에게 보내기"로 전송
- GitHub Actions에서 8시간마다 실행되는 것을 전제로 작성됨

필요 환경변수 (GitHub Secrets):
  KAKAO_REST_API_KEY   카카오 앱의 REST API 키
  KAKAO_REFRESH_TOKEN  최초 1회 발급받은 refresh token
  GEMINI_API_KEY       Google AI Studio에서 무료 발급한 Gemini API 키
  (선택) GH_PAT        refresh token 자동 갱신용 GitHub PAT (repo scope)
  (선택) GH_REPO       "owner/repo" 형식, GH_PAT 사용 시 필요
"""

import os
import sys
import json
import base64
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

MAX_ARTICLES_PER_FEED = 8      # 피드당 가져올 기사 수
GEMINI_MODEL = "gemini-3.5-flash"  # 무료 등급 대상 모델 (2026-05 출시)
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
    """모든 피드를 수집해 프롬프트에 넣을 텍스트 블록으로 변환"""
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
# 2. Gemini API 요약 (무료 등급)
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """당신은 매일 아침 사용자에게 지난 하루의 주요 뉴스를 요약해 보내주는 개인 뉴스 큐레이터입니다.
제공된 뉴스 기사 목록을 바탕으로 하루를 시작하며 읽을 카카오톡 아침 브리핑을 작성하세요.

[규칙]
1. 전체 길이는 공백 포함 500~800자 이내.
2. 실제 기사가 있는 카테고리만 포함하며, 카테고리 앞에 이모지 사용: 🌍 국제 / 💰 경제 / 📌 종합
3. 카테고리별로 가장 중요한 기사 1~2개만 선정. 각 기사는 1줄 헤드라인 + 1~2줄 핵심 요약.
4. 자극적이거나 클릭베이트성 표현 금지. 사실 위주로 담백하게.
5. 중복되거나 사실상 같은 사건을 다루는 기사는 하나로 합침.
6. 마크다운(**, #, - 등) 사용 금지. 순수 텍스트와 이모지만 사용. 줄바꿈으로 구분.
7. 다른 설명이나 인사말 없이 브리핑 본문만 출력."""


def summarize_with_gemini(news_text: str, now_kst: datetime.datetime) -> str:
    api_key = os.environ["GEMINI_API_KEY"]
    user_prompt = (
        f"현재 시각: {now_kst.strftime('%m월 %d일 %H:%M')} (KST)\n"
        f"맨 첫 줄은 \"☀️ 아침 뉴스 브리핑 ({now_kst.strftime('%m월 %d일')})\"으로 시작하세요.\n\n"
        f"[입력 뉴스 데이터]\n{news_text}"
    )
    body = json.dumps({
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": user_prompt}]}],
        "generationConfig": {"maxOutputTokens": 2000},
    }).encode()

    req = request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        data=body,
        headers={
            "x-goog-api-key": api_key,
            "content-type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


# ─────────────────────────────────────────────
# 3. 카카오 토큰 갱신 + 메시지 전송
# ─────────────────────────────────────────────

def refresh_kakao_token() -> tuple[str, str | None]:
    """refresh token으로 access token 발급. 새 refresh token이 오면 함께 반환."""
    payload = parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": os.environ["KAKAO_REST_API_KEY"],
        "refresh_token": os.environ["KAKAO_REFRESH_TOKEN"],
    }).encode()
    req = request.Request("https://kauth.kakao.com/oauth/token", data=payload, method="POST")
    with request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data["access_token"], data.get("refresh_token")


def send_kakao_memo(access_token: str, text: str, link_url: str = "https://news.naver.com"):
    # 기본 텍스트 템플릿은 text 최대 200자 → 초과분은 잘라서 전송하고 전문은 링크 유도 대신
    # 여러 개로 나눠 보낸다.
    chunks = [text[i:i + 190] for i in range(0, len(text), 190)]
    for chunk in chunks:
        template = {
            "object_type": "text",
            "text": chunk,
            "link": {"web_url": link_url, "mobile_web_url": link_url},
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
# 4. (선택) 새 refresh token을 GitHub Secret에 저장
# ─────────────────────────────────────────────

def update_github_secret(new_refresh_token: str):
    """GH_PAT와 GH_REPO가 설정된 경우에만 동작. libsodium 암호화가 필요해
    PyNaCl(pip install pynacl)에 의존한다. 미설치 시 경고만 출력."""
    pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GH_REPO")
    if not pat or not repo:
        print("[warn] 새 refresh token이 발급되었지만 GH_PAT/GH_REPO 미설정으로 자동 저장 불가.")
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

    print("[info] Gemini 요약 중...")
    briefing = summarize_with_gemini(news_text, now_kst)
    print(f"[info] 브리핑 생성 완료 ({len(briefing)}자)")

    print("[info] 카카오 토큰 갱신 중...")
    access_token, new_refresh = refresh_kakao_token()

    print("[info] 카카오톡 전송 중...")
    send_kakao_memo(access_token, briefing)
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
