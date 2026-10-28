"""시장 흐름 브리핑 → 이미지 2장(시장 / 내 계좌) + 대시보드 기록 → 카카오톡 '나에게 보내기'.

  python brief.py auth <인가코드>        최초 1회 카카오 토큰 발급
  python brief.py am|pm [--dry]          전부(수집→관망→이미지→기록→전송). --dry는 전송 안 함
  단계별(Claude 예약 작업용):
  python brief.py fetch am|pm            out/<id>-data.json 에 숫자만 저장
  python brief.py build am|pm [note.json] 이미지 2장 + data/brief.json 기록. note 없으면 자체 관망
  python brief.py send am|pm             이미지 2장 전송
  python brief.py lint note.json am|pm   관망 노트 검사(금지어·없는 숫자)
"""
import csv, datetime as dt, html, json, os, pathlib, re, subprocess, sys
import xml.etree.ElementTree as ET

import requests
import yfinance as yf
from PIL import Image

HERE = pathlib.Path(__file__).parent
# 클라우드(GitHub Actions 실시간 갱신)에는 config.json이 없다 → 비밀번호는 저장소 비밀값(SITE_PASSWORD)으로
CFG = json.loads((HERE / "config.json").read_text("utf-8")) if (HERE / "config.json").exists() else {}
if os.environ.get("SITE_PASSWORD"):
    CFG["site_password"] = os.environ["SITE_PASSWORD"]
TOKEN = HERE / "kakao_token.json"
OUT, HIST = HERE / "out", HERE / "data" / "brief.json"
REDIRECT = "https://localhost"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BG = (238, 241, 234)  # 페이지 배경 #eef1ea — 캡처 후 아래 여백 자르기 기준
KEEP = 120  # 대시보드 기록에 남길 브리핑 수(하루 2번 → 약 2달)

MARKET = {"코스피": "^KS11", "코스닥": "^KQ11", "원/달러": "KRW=X", "S&P500": "^GSPC", "나스닥": "^IXIC",
          "필라델피아반도체": "^SOX", "미 10년물": "^TNX", "WTI": "CL=F", "VIX": "^VIX",
          "이더리움": "ETH-USD", "S&P 선물": "ES=F", "나스닥 선물": "NQ=F"}
ORDER = {"pm": list(MARKET),  # 오후엔 오늘 밤 미국장 분위기를 보려고 선물까지
         "am": ["나스닥", "S&P500", "필라델피아반도체", "미 10년물", "WTI", "VIX", "이더리움", "코스피", "코스닥", "원/달러"]}
NEWS_SKIP = ("stock price", "competitors", "put (", "call (", "interactive stock chart", "options chain", "price today",
             "tokenized", "estimates –", "씽크풀")  # 시세 페이지·옵션 목록·매수 권유성 글
SECTOR_KR = {"반도체": "091160.KS", "2차전지": "305720.KS", "자동차": "091180.KS",
             "은행": "091170.KS", "바이오": "244580.KS", "조선": "466920.KS"}
SECTOR_US = {"기술": "XLK", "금융": "XLF", "에너지": "XLE", "헬스케어": "XLV",
             "산업재": "XLI", "소비재": "XLY"}
BANNED = ["매수", "매도", "손절", "익절", "목표가", "물타기", "사세요", "파세요", "추격"]


def bid(kind):
    return f"{dt.date.today()}-{kind}"


# ---------- 숫자 ----------

OHLCV = None  # 마지막 fetch의 6개월 일봉 — tech()가 차트 분석에 쓴다


def fetch(tickers):
    """{티커: (종가, 전일대비%, 5일%, 일간 변동성%)} — 받지 못한 티커는 빠진다. 변동성은 최근 약 20거래일."""
    global OHLCV
    # 2년: ADX 안정·52주 위치·20일 확률 표본. 병렬이면 yfinance 캐시 DB가 잠겨 일부 티커가 빠짐
    OHLCV = yf.download(list(tickers), period="2y", interval="1d", progress=False, auto_adjust=False, threads=False)
    df = OHLCV["Close"]
    out = {}
    for t in tickers:
        s = df[t].dropna() if t in df else []
        if len(s) >= 2:
            out[t] = (float(s.iloc[-1]), (s.iloc[-1] / s.iloc[-2] - 1) * 100,
                      (s.iloc[-1] / s.iloc[max(-6, -len(s))] - 1) * 100, float(s.pct_change().iloc[-20:].std() * 100),
                      s.index[-1].strftime("%Y-%m-%d"))  # 마지막 봉 날짜 — 시장 휴장일 어긋남 확인용
    return out


INTRA = {"5m": "5d", "30m": "1mo", "60m": "3mo"}  # 분봉 단위: 받을 기간(야후 무료 한도: 5·30분 60일, 60분 730일)
INTRA_DF = {}


def fetch_intraday(tickers):
    for iv, period in INTRA.items():
        try:
            INTRA_DF[iv] = yf.download(list(tickers), period=period, interval=iv, progress=False, auto_adjust=False, threads=False)
        except Exception:
            INTRA_DF[iv] = None


def quote(t, ext=False):
    """거래소 공식 시세(현재가·전일 종가·마지막 체결 시각). 거래가 뜸한 종목은 일봉 종가(마지막 체결가)와 공식 종가가 달라서 쓴다.
    ext=True(실시간용): 프리·애프터마켓 체결이 정규장보다 최근이면 그 가격, 비교 기준은 마지막 정규장 종가. 4번째 값 = 세션 이름."""
    try:
        i = yf.Ticker(t).info
        p, pc, ts = i.get("regularMarketPrice"), i.get("previousClose"), i.get("regularMarketTime")
        if not (p and pc):
            return None
        if ext:
            best = max(((i.get(f"{k}Price"), i.get(f"{k}Time"), name) for k, name in (("preMarket", "프리마켓"), ("postMarket", "애프터마켓"))
                        if i.get(f"{k}Price") and i.get(f"{k}Time")), key=lambda x: x[1], default=None)
            if best and best[1] > ts:
                return float(best[0]), float(p), int(best[1]), best[2]
            return float(p), float(pc), int(ts), "정규장"
        return float(p), float(pc), int(ts)
    except Exception:
        return None


def patch_quote(t):
    """미국 종목: 마지막 일봉 종가 = 공식 현재가, 그 앞 봉 종가 = 공식 전일 종가로 맞춘다. 마지막 체결 시각(KST)을 돌려준다."""
    global OHLCV
    qt = quote(t)
    s = OHLCV["Close"][t].dropna() if t in OHLCV["Close"] else None
    if not qt or s is None or len(s) < 2:
        return None
    OHLCV.loc[s.index[-1], ("Close", t)] = qt[0]
    OHLCV.loc[s.index[-2], ("Close", t)] = qt[1]
    return dt.datetime.fromtimestamp(qt[2]).strftime("%m-%d %H:%M")


def patch_daily(t):
    """야후 일봉에 마지막 거래일 값이 비어 있으면(국내 종목에서 자주 생김) 5분봉으로 그날 봉을 만들어 채운다.
    5분봉은 장 마감 동시호가 직전까지라 종가는 '마지막 체결가'에 가깝다. 채웠으면 True."""
    import pandas as pd
    global OHLCV
    m5 = INTRA_DF.get("5m")
    if m5 is None or t not in m5["Close"]:
        return False
    f = pd.DataFrame({x: m5[x][t] for x in ("Open", "High", "Low", "Close", "Volume")}).dropna()
    if f.empty:
        return False
    tz = "Asia/Seoul" if t.endswith((".KS", ".KQ")) else "America/New_York"
    idx = f.index.tz_convert(tz) if f.index.tz is not None else f.index
    day = idx[-1].date()
    s = OHLCV["Close"][t].dropna()
    if not s.empty and s.index[-1].date() >= day:
        return False
    g = f[[i.date() == day for i in idx]]
    row = {("Open", t): g["Open"].iloc[0], ("High", t): g["High"].max(), ("Low", t): g["Low"].min(),
           ("Close", t): g["Close"].iloc[-1], ("Adj Close", t): g["Close"].iloc[-1], ("Volume", t): g["Volume"].sum()}
    ts = pd.Timestamp(day)
    if ts not in OHLCV.index:
        OHLCV = pd.concat([OHLCV, pd.DataFrame(index=[ts], columns=OHLCV.columns)]).sort_index()
    for k, v in row.items():
        if k in OHLCV.columns:
            OHLCV.loc[ts, k] = v
    OHLCV = OHLCV.astype(float)
    return True


def tech_tf(t, iv, bars=90):
    """분봉 차트 분석 — 일봉과 같은 계산을 그 단위에 적용(52주·지수 대비는 의미 없어 뺌)."""
    df = INTRA_DF.get(iv)
    if df is None or t not in df["Close"]:
        return None
    x = tech(t, None, bars=bars, k=3, src=df)
    if x:
        for key in ("hi52", "lo52", "from_hi", "from_lo", "rs"):
            x.pop(key, None)
        stamp = "%m-%d %H:%M"
        f = df["Close"][t].dropna().iloc[-bars:]
        x["ohlc"] = [[i.tz_convert("Asia/Seoul").strftime(stamp) if i.tzinfo else i.strftime(stamp)] + b[1:]
                     for i, b in zip(f.index, x["ohlc"])]
    return x


def tech(t, idx=None, bars=120, k=5, src=None):
    """차트 분석 지표 + 최근 bars봉 캔들. 데이터가 모자라면 None. src를 주면 그 봉 데이터(분봉)로 계산.
    근거: chart-analysis-study.md(StockCharts ChartSchool 등) — ATR·ADX는 Wilder 평활, 지지·저항은 k봉 스윙 고저를
    0.75 ATR 안에서 묶은 구간(터치 2회 이상), 무효화 = 아래 지지 구간 하단 −0.5 ATR.
    ponytail: k=5·0.75ATR·스퀴즈 15백분위는 출발 가정값, 보유 종목 과거 차트로 검증해 조정할 것."""
    import numpy as np
    import pandas as pd
    src = OHLCV if src is None else src
    f = pd.DataFrame({x: src[x][t] for x in ("Open", "High", "Low", "Close", "Volume")}).dropna()
    if len(f) < 40:
        return None
    o, c, h, l, v = f["Open"], f["Close"], f["High"], f["Low"], f["Volume"]
    px = float(c.iloc[-1])
    r = lambda x: None if x is None else round(float(x), 0 if px >= 1000 else 2 if px >= 1 else 4)
    w = lambda s: s.ewm(alpha=1 / 14, adjust=False).mean()  # Wilder 평활
    ma = {n: float(c.rolling(n).mean().iloc[-1]) for n in (5, 20, 60) if len(c) >= n}
    d = c.diff()
    rsi_s = 100 - 100 / (1 + w(d.clip(lower=0)) / w(-d.clip(upper=0)).replace(0, np.nan))
    rsi_s = rsi_s.fillna(100)
    macd = c.ewm(span=12).mean() - c.ewm(span=26).mean()
    sig = macd.ewm(span=9).mean()
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = float(w(tr).iloc[-1])
    upm, dnm = h.diff(), -l.diff()
    pdi = 100 * w(upm.where((upm > dnm) & (upm > 0), 0.0)) / w(tr)
    mdi = 100 * w(dnm.where((dnm > upm) & (dnm > 0), 0.0)) / w(tr)
    adx = float(w(100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).iloc[-1])
    regime = ("추세 강함" if adx > 25 else "추세 없음(횡보)" if adx < 20 else "추세 약함") + \
             (" · 상승 우위" if pdi.iloc[-1] > mdi.iloc[-1] else " · 하락 우위")
    # 스윙 고저(좌우 k봉보다 높은/낮은 봉) — 확정까지 k봉 늦다
    hs = [i for i in range(k, len(f) - k) if h.iloc[i] == h.iloc[i - k:i + k + 1].max()]
    ls = [i for i in range(k, len(f) - k) if l.iloc[i] == l.iloc[i - k:i + k + 1].min()]
    zones = []  # 최근 1년 스윙만, 구간 폭은 1 ATR 이내(사슬처럼 이어 붙어 거대 구간이 되지 않게)
    recent = len(f) - 252
    for p in sorted([float(h.iloc[i]) for i in hs if i >= recent] + [float(l.iloc[i]) for i in ls if i >= recent]):
        if zones and p - zones[-1][0] <= 1.0 * atr:
            zones[-1][1], zones[-1][2] = p, zones[-1][2] + 1
        else:
            zones.append([p, p, 1])
    zones = [z for z in zones if z[2] >= 2]
    # 반 ATR보다 가까운 구간은 '지금 시험 중'이라 시나리오 레벨로 쓰지 않는다. 없으면 ±2 ATR(가격대 없음 표시 n=0)
    above = [z for z in zones if z[0] > px + 0.5 * atr]
    below = [z for z in zones if z[1] < px - 0.5 * atr]
    res_z = min(above, key=lambda z: z[0]) if above else [px + 2 * atr] * 2 + [0]
    sup_z = max(below, key=lambda z: z[1]) if below else [px - 2 * atr] * 2 + [0]
    testing = [z for z in zones if z[0] - 0.5 * atr <= px <= z[1] + 0.5 * atr]
    # 추세 구조: 마지막 두 스윙 고점·저점
    hh = len(hs) >= 2 and h.iloc[hs[-1]] > h.iloc[hs[-2]]
    hl = len(ls) >= 2 and l.iloc[ls[-1]] > l.iloc[ls[-2]]
    struct = "고점·저점 상승" if hh and hl else "고점·저점 하락" if not hh and not hl and len(hs) >= 2 and len(ls) >= 2 else "엇갈림"
    div = None  # 다이버전스는 가격이 확인하기 전까지 '경고'
    if len(ls) >= 2 and l.iloc[ls[-1]] < l.iloc[ls[-2]] and rsi_s.iloc[ls[-1]] > rsi_s.iloc[ls[-2]] and len(f) - ls[-1] <= 30:
        div = "강세 다이버전스 경고"
    elif len(hs) >= 2 and h.iloc[hs[-1]] > h.iloc[hs[-2]] and rsi_s.iloc[hs[-1]] < rsi_s.iloc[hs[-2]] and len(f) - hs[-1] <= 30:
        div = "약세 다이버전스 경고"
    bw = (4 * c.rolling(20).std() / c.rolling(20).mean() * 100).dropna()
    bw_pct = float((bw.iloc[-200:] < bw.iloc[-1]).mean() * 100) if len(bw) > 40 else None
    obv = (np.sign(d).fillna(0) * v).cumsum()
    obv_up, px_up = obv.iloc[-1] > obv.iloc[-21], c.iloc[-1] > c.iloc[-21]
    obv_state = "가격과 같은 방향" if obv_up == px_up else ("가격 하락 중 OBV 상승(매집 괴리)" if obv_up else "가격 상승 중 OBV 하락(분산 괴리)")
    rs = None
    if idx and idx in OHLCV["Close"]:
        ic = OHLCV["Close"][idx].reindex(c.index).ffill()  # 날짜로 맞춘다(휴장일이 달라도 같은 기간)
        if ic.iloc[-21:].notna().all():
            rs = round(((c.iloc[-1] / c.iloc[-21]) / (ic.iloc[-1] / ic.iloc[-21]) - 1) * 100, 1)
    hi52, lo52 = float(h.iloc[-252:].max()), float(l.iloc[-252:].min())
    trend = "정배열" if 60 in ma and px > ma[20] > ma[60] else "역배열" if 60 in ma and px < ma[20] < ma[60] else "혼조"
    cross = "골든" if macd.iloc[-1] > sig.iloc[-1] and macd.iloc[-2] <= sig.iloc[-2] else \
            "데드" if macd.iloc[-1] < sig.iloc[-1] and macd.iloc[-2] >= sig.iloc[-2] else None
    vavg = v.iloc[-21:-1].mean()
    tail = f.iloc[-bars:]
    return {"price": r(px), "ma5": r(ma[5]), "ma20": r(ma.get(20, px)), "ma60": r(ma.get(60)),
            "dist_ma20": round((px / ma[20] - 1) * 100, 1) if 20 in ma else None,
            "rsi": round(float(rsi_s.iloc[-1])), "macd_above": bool(macd.iloc[-1] > sig.iloc[-1]), "macd_cross": cross,
            "atr": r(atr), "atr_pct": round(atr / px * 100, 1), "range_lo": r(px - atr), "range_hi": r(px + atr),
            "adx": round(adx), "regime": regime, "struct": struct, "div": div, "obv": obv_state, "rs": rs,
            "squeeze": bw_pct is not None and bw_pct <= 15, "bw_pct": None if bw_pct is None else round(bw_pct),
            "sup": r(sup_z[1]), "sup_lo": r(sup_z[0]), "sup_n": sup_z[2], "res": r(res_z[0]), "res_hi": r(res_z[1]), "res_n": res_z[2],
            "invalid": r(sup_z[0] - 0.5 * atr),
            "testing": [r(testing[0][0]), r(testing[0][1])] if testing else None,
            "zones": [[r(z[0]), r(z[1]), z[2]] for z in zones if z[0] <= float(tail["High"].max()) and z[1] >= float(tail["Low"].min())],
            "hi52": r(hi52), "lo52": r(lo52), "from_hi": round((px / hi52 - 1) * 100, 1), "from_lo": round((px / lo52 - 1) * 100, 1),
            "vol_x": round(float(v.iloc[-1] / vavg), 1) if vavg else None, "trend": trend,
            "ohlc": [[i.strftime("%m-%d"), r(a), r(b), r(e), r(g), int(vv)]
                     for i, a, b, e, g, vv in zip(tail.index, tail["Open"], tail["High"], tail["Low"], tail["Close"], tail["Volume"])]}


# ---------- 시나리오 확률 (scenario-prob-study.md (B)) ----------
PROB_M = 20          # 풀(보유 종목 묶음) 쪽으로 당기는 세기 — ponytail: 제안값, 채점 쌓이면 교정
DILUTE = ("CB 발행", "CB발행", "전환사채", "유상증자", "offering", "dilut", "at-the-market", "ATM program")  # 'CB' 단독은 CBC뉴스 같은 언론사명에 걸림


def _series(t):
    """종가·ATR·ADX≥25 여부 시계열(일봉)."""
    import numpy as np
    import pandas as pd
    f = pd.DataFrame({x: OHLCV[x][t] for x in ("High", "Low", "Close")}).dropna()
    c, h, l = f["Close"], f["High"], f["Low"]
    w = lambda s: s.ewm(alpha=1 / 14, adjust=False).mean()
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    upm, dnm = h.diff(), -l.diff()
    pdi = 100 * w(upm.where((upm > dnm) & (upm > 0), 0.0)) / w(tr)
    mdi = 100 * w(dnm.where((dnm > upm) & (dnm > 0), 0.0)) / w(tr)
    adx = w(100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan))
    return c.to_numpy(), w(tr).to_numpy(), (adx >= 25).to_numpy()


def first_passage(t, du, dd, n, regime=None):
    """과거 각 날에 지금과 같은 ATR 거리(du 위, dd 아래)의 선을 긋고 n일 안에 종가가 먼저 닿은 쪽을 센다.
    반환 (위 먼저, 아래 먼저, 표본 수, 겹치지 않게 센 표본 수). regime을 주면 그날 ADX≥25 여부가 같은 날만."""
    c, atr, flag = _series(t)
    up = dn = cnt = nono = 0
    last = -n
    for i in range(30, len(c) - n):
        if regime is not None and flag[i] != regime:
            continue
        if i - last >= n:  # 앞 표본과 기간이 안 겹치는 것만 따로 센다
            nono, last = nono + 1, i
        hi, lo = c[i] + du * atr[i], c[i] - dd * atr[i]
        for x in c[i + 1:i + n + 1]:
            if x >= hi:
                up += 1
                break
            if x <= lo:
                dn += 1
                break
        cnt += 1
    return up, dn, cnt, nono


def data_ok(t, need=120):
    """GPT 3차 ⑤: 봉 수·가격>0·고가≥저가 검사. 실패하면 확률을 내지 않는다."""
    import pandas as pd
    f = pd.DataFrame({x: OHLCV[x][t] for x in ("High", "Low", "Close")}).dropna()
    return len(f) >= need and (f > 0).all().all() and (f["High"] >= f["Low"]).all()


def chart_prob(inst, T, pool):
    """차트 기준 확률 {5: {...}, 20: {...}}: 자기 종목 비율을 풀 비율 쪽으로 PROB_M만큼 당긴다. 겹치는 창이라 유효 표본 = n/N."""
    if not T or not T.get("atr") or T["atr"] != T["atr"] or T.get("adx") is None or T["adx"] != T["adx"] or not data_ok(inst):
        return None  # 흔들림 폭·추세 강도가 비거나 NaN이면 확률을 내지 않는다
    pool = [p for p in pool if data_ok(p)]
    du, dd = (T["res"] - T["price"]) / T["atr"], (T["price"] - T["sup_lo"]) / T["atr"]
    regime = T["adx"] >= 25
    out = {}
    for n in (5, 20):
        # 흐름 조건(ADX) 필터는 5일에만: 20일은 겹치지 않는 표본이 너무 적어진다(GPT 점검 반영)
        reg = regime if n == 5 else None
        own = first_passage(inst, du, dd, n, reg)
        relaxed = reg is not None and own[3] < 15
        if relaxed:
            own = first_passage(inst, du, dd, n)
        ps = [first_passage(p, du, dd, n, None if relaxed else reg) for p in pool]
        pu, pd_, pn = (sum(x[i] for x in ps) for i in range(3))
        pool_up, pool_dn = (pu / pn, pd_ / pn) if pn else (1 / 3, 1 / 3)
        ne = own[3]  # 겹치지 않는 표본 수를 유효 표본으로
        up = (own[0] / max(own[2], 1) * ne + PROB_M * pool_up) / (ne + PROB_M)
        dn = (own[1] / max(own[2], 1) * ne + PROB_M * pool_dn) / (ne + PROB_M)
        k = ne + PROB_M
        # '참고 범위' — 신뢰구간이 아니라 표본 크기에 따른 흔들림의 대략치
        rng = lambda p: [max(0, round((p - 1.28 * (p * (1 - p) / k) ** .5) * 100)), min(100, round((p + 1.28 * (p * (1 - p) / k) ** .5) * 100))]
        out[str(n)] = {"up": round(up * 100), "dn": round(dn * 100), "base": 100 - round(up * 100) - round(dn * 100),
                       "up_rng": rng(up), "dn_rng": rng(dn), "n_eff": ne, "n_all": own[2], "pool_eff": sum(x[3] for x in ps),
                       "relaxed": relaxed, "regime_used": reg is not None and not relaxed,
                       "thin": ne < 15 or sum(x[3] for x in ps) < 30}
    return {"levels": {"up": T["res"], "dn": T["sup_lo"]}, "inst": inst, "h": out}


# 방향 판정 걷기 검증(2026-10-28, 5년·국내외 25종목, 그날까지 결과가 나온 과거만 써서 예측): 동전 던지기와 차이 없음.
# 다른 신호 묶음(과열·과매도, 평균 이탈, 120일 추세)도 44~55%로 같았다. 실전 성적은 score()가 따로 쌓는다.
DIR_CHECK = {"at": "2026-10-28", "years": 5, "stocks": 25, "n5": 3251, "hit5": 49.5, "n20": 809, "hit20": 47.2}
DIR_SIGNALS = ["주가가 한 달 평균보다 위", "한 달 평균이 석 달 평균보다 위", "단기 힘(MACD)이 오르는 쪽", "한 달 전보다 높음"]


def _trend_signals(c):
    """차트 신호 4개(불리언 시계열)와 점수(0~4, 석 달 평균이 생긴 뒤부터)."""
    ema = lambda s, n: s.ewm(span=n, adjust=False).mean()
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    macd = ema(c, 12) - ema(c, 26)
    sig = [c > ma20, ma20 > ma60, macd > ema(macd, 9), c > c.shift(20)]
    return sig, sum(s.astype(int) for s in sig).where(ma60.notna())


def direction(inst, pool):
    """차트 판정: 지금 신호 상태 + 같은 상태였던 과거(자기 + 같은 시장 풀) N일 뒤 지금보다 높았던 비율(PROB_M으로 풀 쪽으로 당김)."""
    import pandas as pd
    c = OHLCV["Close"][inst].dropna()
    sig, s = _trend_signals(c)
    if len(c) < 120 or pd.isna(s.iloc[-1]):
        return None
    now = int(s.iloc[-1])

    def rate(t, n):
        c = OHLCV["Close"][t].dropna()
        s, fwd = _trend_signals(c)[1], c.shift(-n)
        ok = s.notna() & fwd.notna()
        m = ok & (s == now)
        return int((fwd[m] > c[m]).sum()), int(m.sum()), float((fwd[ok] > c[ok]).mean()) if ok.any() else None

    out = {}
    for n in (5, 20):
        ou, on, base = rate(inst, n)
        ps = [rate(p, n) for p in pool if p in OHLCV["Close"] and data_ok(p)]
        pu, pn = sum(x[0] for x in ps), sum(x[1] for x in ps)
        pool_r = pu / pn if pn else 0.5
        ne = on / n  # 겹치는 창 → 유효 표본
        p = ((ou / on if on else pool_r) * ne + PROB_M * pool_r) / (ne + PROB_M)
        out[str(n)] = {"up": round(p * 100), "base": None if base is None else round(base * 100), "n": on, "n_eff": round(ne)}
    return {"inst": inst, "score": now, "signals": [[k, bool(x.iloc[-1])] for k, x in zip(DIR_SIGNALS, sig)],
            "state": "오르는 흐름" if now >= 3 else "내리는 흐름" if now <= 1 else "엇갈린 흐름", "p": out, "check": DIR_CHECK}


def outlook_prob(cp, tone, inputs, n):
    """전망 반영 확률: 차트 확률을 로그오즈로 바꿔 요인을 상한 안에서 더한다(Δ 방향, V 양쪽 꼬리)."""
    import math
    p = cp["h"][str(n)]
    pu, pdn, pb = max(p["up"], 1) / 100, max(p["dn"], 1) / 100, max(p["base"], 1) / 100
    fac, delta, vol = [], 0.0, 0.0
    if n == 5 and tone in ("우호", "비우호"):  # 뉴스 흐름은 짧은 기간에만
        s = 1 if tone == "우호" else -1
        delta += 0.15 * s
        fac.append(f"뉴스 흐름 {tone}({'+' if s > 0 else '−'})")
    if inputs.get("mkt") is not None:
        s = inputs["mkt"]
        delta += 0.20 * s
        fac.append(f"지수·상대강도 {'우호' if s > 0 else '비우호' if s < 0 else '중립'}({'+' if s > 0 else '−' if s < 0 else '0'})")
    if inputs.get("dilute"):
        delta -= 0.25
        fac.append("증자·CB 등 희석 이슈(−)")
    if inputs.get("earn_days") is not None and 0 <= inputs["earn_days"] <= n:
        vol += 0.35
        fac.append("기간 안 실적 발표(위·아래 모두 커짐)")
    delta = max(-0.5, min(0.5, delta))
    vol = min(0.6, vol)
    zu, zd = math.log(pu / pb) + delta + vol, math.log(pdn / pb) - delta + vol
    s = math.exp(zu) + math.exp(zd) + 1
    up, dn = round(math.exp(zu) / s * 100), round(math.exp(zd) / s * 100)
    return {"up": up, "dn": dn, "base": 100 - up - dn, "factors": fac}


def score():
    """예측 기록(data/predictions.jsonl)을 실제 종가로 채점한다(3갈래 Brier, 낮을수록 좋음, 아무렇게나 찍으면 약 0.667).
    같은 종목·같은 기간은 앞 예측과 기간이 겹치면 빼서 독립 표본만 센다(GPT 2차 점검 ③)."""
    import pandas as pd
    p = HERE / "data" / "predictions.jsonl"
    recs = [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()] if p.exists() else []
    if not recs:
        return print("채점할 예측 기록이 없습니다")
    start = (dt.date.fromisoformat(min(r["id"][:10] for r in recs)) - dt.timedelta(days=7)).isoformat()  # 가장 오래된 예측일부터
    closes = yf.download(sorted({r["inst"] for r in recs}), start=start, interval="1d", progress=False,
                         auto_adjust=False, threads=False)["Close"]
    brier = lambda pr, o: sum((x / 100 - y) ** 2 for x, y in zip(pr, o))
    out, last, pend = {"5": [], "20": []}, {}, {}
    for r in sorted(recs, key=lambda r: r["id"]):
        d0, kind = r["id"][:10], r["id"][11:]
        s = closes[r["inst"]].dropna() if r["inst"] in closes else pd.Series(dtype=float)
        # 기준 종가 다음 거래일부터: 국내 오후 브리핑은 당일 종가가 기준이라 다음 날부터, 나머지는 당일부터
        fut = s[s.index.strftime("%Y-%m-%d") > d0] if r["inst"].endswith((".KS", ".KQ")) and kind == "pm" else \
              s[s.index.strftime("%Y-%m-%d") >= d0]
        for n in ("5", "20"):
            k = (r["inst"], n)
            # 앞 예측의 평가 마지막 거래일 이후에 시작한 예측만(겹침 없는 독립 표본)
            if k in last and pd.Timestamp(d0) <= last[k]:
                continue
            if len(fut) < int(n):
                if n == "5" and r["name"] not in pend:  # 아직 기간이 안 끝난 5일 예측: 중간 경과(홈페이지 '진행 중')
                    hit = next(("위" if x >= r["levels"]["up"] else "아래" for x in fut
                                if x >= r["levels"]["up"] or x <= r["levels"]["dn"]), None)
                    c = r["chart"]["5"]
                    pend[r["name"]] = {"name": r["name"], "id": r["id"], "days": len(fut), "hit": hit,
                                       "now": round(float(fut.iloc[-1]), 4) if len(fut) else None,
                                       "inst": r["inst"], "up": r["levels"]["up"], "dn": r["levels"]["dn"],
                                       "top": "·".join(o for o, v in zip(["위", "둘 다 아님", "아래"], c) if v == max(c)), "top_p": max(c),
                                       "price": r["price"], "dir_p": r.get("dir", {}).get("5")}
                continue
            last[k] = fut.index[int(n) - 1].tz_localize(None) if fut.index.tz is not None else fut.index[int(n) - 1]
            hit = [0, 1, 0]  # 위 먼저, 둘 다 아님, 아래 먼저
            for x in fut.iloc[:int(n)]:
                if x >= r["levels"]["up"]:
                    hit = [1, 0, 0]
                    break
                if x <= r["levels"]["dn"]:
                    hit = [0, 0, 1]
                    break
            item = {"id": r["id"], "name": r["name"], "hit": hit,
                    "chart": brier(r["chart"][n], hit), "outlook": brier(r["outlook"][n], hit)}
            if "dir" in r:  # 방향 판정: n거래일 뒤 종가가 예측 때보다 높았나(2갈래 Brier, 반반 찍기 0.25)
                item["dir"], item["rose"] = r["dir"][n], int(float(fut.iloc[int(n) - 1]) > r["price"])
            out[n].append(item)
    summary = {}
    for n, v in out.items():
        dv = [x for x in v if "dir" in x]
        called = [x for x in dv if x["dir"] != 50]
        summary[n] = {"n": len(v), "chart": round(sum(x["chart"] for x in v) / len(v), 3) if v else None,
                      "outlook": round(sum(x["outlook"] for x in v) / len(v), 3) if v else None, "uniform": 0.667,
                      "dir": {"n": len(dv), "brier": round(sum((x["dir"] / 100 - x["rose"]) ** 2 for x in dv) / len(dv), 3) if dv else None,
                              "hit": round(sum((x["dir"] > 50) == bool(x["rose"]) for x in called) / len(called) * 100) if called else None,
                              "uniform": 0.25}}
    (HERE / "data" / "scores.json").write_text(json.dumps({"at": dt.datetime.now().isoformat(timespec="minutes"),
                                                          "summary": summary, "items": out, "pending": list(pend.values())},
                                                         ensure_ascii=False), "utf-8")
    print(summary)


def short_pct(t):
    """미국 종목 공매도 비율(유통주식 대비 %). 없거나 실패하면 None."""
    try:
        v = yf.Ticker(t).info.get("shortPercentOfFloat")
        return round(v * 100, 1) if v else None
    except Exception:
        return None


def earnings(holds, days=35):  # 20거래일 시나리오(≈28일)를 덮도록
    """보유 종목 중 days일 안에 실적 발표가 잡힌 것. ETF·조회 실패는 건너뛴다."""
    out, today = [], dt.date.today()
    for name, t in holds:
        try:
            dates = (yf.Ticker(t).calendar or {}).get("Earnings Date") or []
        except Exception:
            continue
        if dates and 0 <= (dates[0] - today).days <= days:
            out.append({"name": name, "date": dates[0].isoformat()})
    return sorted(out, key=lambda e: e["date"])


UA = {"User-Agent": "Mozilla/5.0"}


def flows(code, days=5):
    """국내 종목 최근 days일 외국인·기관·개인 순매수(주)와 외국인 보유율 — 네이버 모바일 데이터. 실패하면 None."""
    try:
        rows = requests.get(f"https://m.stock.naver.com/api/stock/{code}/trend?pageSize={days}", headers=UA, timeout=15).json()[:days]
    except Exception:
        return None
    n = lambda s: int(str(s).replace(",", "").replace("+", "") or 0)
    if not rows:
        return None
    return {"days": len(rows), "from": rows[-1]["bizdate"], "to": rows[0]["bizdate"],
            "foreign": sum(n(r["foreignerPureBuyQuant"]) for r in rows), "organ": sum(n(r["organPureBuyQuant"]) for r in rows),
            "indiv": sum(n(r["individualPureBuyQuant"]) for r in rows), "foreign_ratio": rows[0].get("foreignerHoldRatio")}


def earn_react(t, k=6):
    """지난 실적 발표 때 반응: 장 마감 뒤 발표면 다음 거래일, 장 전 발표면 그날의 종가 변동(%). 최근 k번."""
    try:
        e = yf.Ticker(t).get_earnings_dates(limit=16)
    except Exception:
        return None
    if e is None or t not in OHLCV["Close"]:
        return None
    c = OHLCV["Close"][t].dropna()
    out = []
    for ts in e.index:
        local = ts.tz_convert("America/New_York") if ts.tzinfo else ts
        day = local.normalize().tz_localize(None)
        if day >= c.index[-1] or day < c.index[0]:
            continue
        after = local.hour >= 12  # 오후(장 마감 뒤) 발표 → 다음 거래일 반응
        i = c.index.searchsorted(day)
        j = i + 1 if after or (i < len(c) and c.index[i] != day) else i
        if j >= len(c) or j < 1:
            continue
        out.append({"date": day.strftime("%Y-%m-%d"), "move": round((c.iloc[j] / c.iloc[j - 1] - 1) * 100, 1)})
        if len(out) == k:
            break
    if not out:
        return None
    return {"items": out, "avg_abs": round(sum(abs(x["move"]) for x in out) / len(out), 1)}


RISKY_FORMS = ("S-1", "S-3", "F-1", "F-3", "424B")  # 미국: 증자·신주 발행 관련 서류
FORM_KO = {"4": "임원·대주주 주식 거래 신고", "3": "새 내부자 등록", "144": "내부자 매도 예정 신고", "8-K": "주요 사항 보고",
           "10-Q": "분기 보고서", "10-K": "연간 보고서", "6-K": "외국 기업 수시 보고", "20-F": "외국 기업 연간 보고서",
           "S-1": "신규 증권 발행 등록(증자 가능)", "S-3": "추가 증권 발행 등록(증자 가능)", "F-1": "외국 기업 증권 발행 등록(증자 가능)",
           "F-3": "외국 기업 추가 발행 등록(증자 가능)", "424B": "신주 발행 확정 설명서(증자)", "SC 13G": "5% 이상 보유 신고",
           "SC 13D": "5% 이상 보유·경영 참여 신고", "DEF 14A": "주주총회 안내", "S-8": "임직원 주식 보상 등록"}
form_ko = lambda f: next((v for k, v in FORM_KO.items() if f == k or (k in ("424B", "SC 13G", "SC 13D") and f.startswith(k))), f)
RISKY_KR = ("유상증자", "전환사채", "신주인수권", "교환사채", "감자", "관리종목", "거래정지", "상장폐지", "불성실공시", "감사의견")


def sec_filings(t, days=14):
    """미국 종목 최근 days일 SEC 공시. 증자 관련 서류는 risky=True. 실패하면 None."""
    if not CFG.get("sec_user_agent"):  # SEC는 연락 이메일이 든 User-Agent를 요구 — config.json에 넣어야 켜짐
        return None
    try:
        hdr = {"User-Agent": CFG["sec_user_agent"]}
        cache = HERE / "data" / "sec_tickers.json"
        if not cache.exists() or (dt.datetime.now().timestamp() - cache.stat().st_mtime) > 7 * 86400:
            r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=hdr, timeout=20)
            r.raise_for_status()  # 거부 응답(403 HTML)을 캐시에 저장하지 않게
            cache.write_text(r.text, "utf-8")
        cik = next((v["cik_str"] for v in json.loads(cache.read_text("utf-8")).values() if v["ticker"] == t), None)
        if cik is None:
            return None
        rec = requests.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers=hdr, timeout=20).json()["filings"]["recent"]
    except Exception:
        return None
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = []
    for form, date, acc, doc in zip(rec["form"], rec["filingDate"], rec["accessionNumber"], rec["primaryDocument"]):
        if date < since:
            break
        out.append({"date": date, "title": f"{form_ko(form)} ({form})", "risky": form.startswith(RISKY_FORMS),
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"})
    return out[:8]


def dart_filings(code, days=14):
    """국내 종목 최근 days일 DART 공시(config.json dart_key 필요). 위험 공시는 risky=True. 키가 없거나 실패하면 None."""
    import io, zipfile
    key = CFG.get("dart_key")
    if not key:
        return None
    try:
        cache = HERE / "data" / "dart_corp.json"
        if not cache.exists():
            z = zipfile.ZipFile(io.BytesIO(requests.get(f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={key}", timeout=60).content))
            root = ET.fromstring(z.read(z.namelist()[0]))
            cache.write_text(json.dumps({x.findtext("stock_code").strip(): x.findtext("corp_code") for x in root.iter("list")
                                         if (x.findtext("stock_code") or "").strip()}), "utf-8")
        corp = json.loads(cache.read_text("utf-8")).get(code)
        if not corp:
            return None
        r = requests.get("https://opendart.fss.or.kr/api/list.json", timeout=20, params={
            "crtfc_key": key, "corp_code": corp, "page_count": 20,
            "bgn_de": (dt.date.today() - dt.timedelta(days=days)).strftime("%Y%m%d"), "end_de": dt.date.today().strftime("%Y%m%d")}).json()
    except Exception:
        return None
    return [{"date": f"{x['rcept_dt'][:4]}-{x['rcept_dt'][4:6]}-{x['rcept_dt'][6:]}", "title": x["report_nm"].strip(),
             "risky": any(k in x["report_nm"] for k in RISKY_KR), "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={x['rcept_no']}"}
            for x in r.get("list", [])][:8]


def news(query, korean, n=4):
    """구글 뉴스 RSS 최근 3일 기사 [{title, source, url, at}]. 실패하면 빈 목록."""
    hl = "hl=ko&gl=KR&ceid=KR:ko" if korean else "hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(f"https://news.google.com/rss/search?q={requests.utils.quote(query + ' when:3d')}&{hl}", timeout=20)
        items = ET.fromstring(r.content).findall(".//item")
    except Exception:
        return []
    out = []
    for it in items:
        title = it.findtext("title") or ""
        if any(s in title.lower() for s in NEWS_SKIP):
            continue
        out.append({"title": title, "source": it.findtext("source"), "url": it.findtext("link"), "at": it.findtext("pubDate")})
        if len(out) == n:
            break
    return out


def collect(kind):
    """시장·섹터·보유·계좌 숫자를 한 dict로. 소수점은 여기서 정해 이미지·대시보드·관망이 같은 값을 쓴다."""
    p = HERE / "holdings.csv"
    hs = [r for r in csv.DictReader(p.open(encoding="utf-8-sig")) if r.get("티커", "").strip()] if p.exists() else []
    sectors = SECTOR_US if kind == "am" else SECTOR_KR
    bases = {(h.get("기초") or "").strip() for h in hs} - {""}
    q = fetch(set(MARKET.values()) | set(sectors.values()) | {h["티커"].strip() for h in hs} | bases)
    kr_mkt = {t for t in set(MARKET.values()) | set(sectors.values()) if t.endswith((".KS", ".KQ")) or t in ("^KS11", "^KQ11")}
    fetch_intraday({h["티커"].strip() for h in hs} | kr_mkt)  # 국내 지수·업종도: 야후 일봉이 하루 늦는 경우를 분봉으로 메움
    last_trade = {}
    for t in {h["티커"].strip() for h in hs} | bases:
        if not t.endswith((".KS", ".KQ")) and (lt := patch_quote(t)):
            last_trade[t] = lt
            s = OHLCV["Close"][t].dropna()
            q[t] = (float(s.iloc[-1]), (s.iloc[-1] / s.iloc[-2] - 1) * 100, (s.iloc[-1] / s.iloc[-6] - 1) * 100,
                    float(s.pct_change().iloc[-20:].std() * 100), s.index[-1].strftime("%Y-%m-%d"))
    for t in {h["티커"].strip() for h in hs} | kr_mkt:
        if patch_daily(t):
            s = OHLCV["Close"][t].dropna()
            q[t] = (float(s.iloc[-1]), (s.iloc[-1] / s.iloc[-2] - 1) * 100, (s.iloc[-1] / s.iloc[-6] - 1) * 100,
                    float(s.pct_change().iloc[-20:].std() * 100), s.index[-1].strftime("%Y-%m-%d"))
    fx = q.get("KRW=X", (1400,))[0]  # ponytail: 환율 조회 실패 시 1400 대체 — 화면에 fx_ok=False로 표시
    missing = [h["종목명"] for h in hs if h["티커"].strip() not in q]  # 시세를 못 받은 종목 — 합계에서 빠짐을 화면에 표시
    mkt_last = {m: q[i][4] for m, i in (("kr", "^KQ11"), ("us", "^IXIC")) if i in q}
    hold = []  # 원금을 비우면 손익은 빼고 오늘 등락만
    for h in hs:
        if (t := h["티커"].strip()) in q:
            qty, cost, base = float(h["수량"]), float(h["원금"] or 0), (h.get("기초") or "").strip()
            kr = t.endswith((".KS", ".KQ"))
            won = q[t][0] * qty * (1 if kr else fx)
            # 그 시장 지수보다 마지막 봉이 오래됐으면(이 종목만 쉰 날) 오늘 손익엔 넣지 않는다
            fresh = q[t][4] >= mkt_last.get("kr" if kr else "us", q[t][4])
            hold.append({"name": h["종목명"], "ticker": t, "qty": qty, "won": round(won), "chg": round(q[t][1], 2), "fresh": fresh,
                         "last_trade": last_trade.get(t),  # 공식 시세의 마지막 체결 시각(KST)
                         "chg5": round(q[t][2], 1), "cost": cost, "pnl": round((won / cost - 1) * 100, 1) if cost else None,
                         "base": base or None, "base_chg": round(q[base][1], 2) if base in q else None,
                         # 2배 일간 리셋 ETF: 기초 일간 변동성 σ면 한 달(21일) 손실 근사 ≈ σ²×21 (L(L-1)/2 = 1)
                         "decay": round(q[base][3] ** 2 * 21 / 100, 1) if base in q else None,
                         "short": None if kr else short_pct(base or t),
                         "tech": tech(t, "^KQ11" if kr else "^IXIC"),
                         "tf": {iv: tech_tf(t, iv) for iv in INTRA},
                         "base_tech": {k: v for k, v in (tech(base, "^IXIC") or {}).items() if k not in ("ohlc", "zones")} if base else None,
                         "flows": flows(t.split(".")[0]) if kr else None,
                         "earn_react": None if kr else earn_react(base or t),
                         "filings": dart_filings(t.split(".")[0]) if kr else sec_filings(base or t),
                         "news": news((h.get("검색어") or "").strip() or (h["종목명"] if kr else f"{base or t} stock"), kr)})
    hold.sort(key=lambda h: -h["won"])
    # 시나리오 확률: 레벨을 쓰는 종목(레버리지 ETF는 기초 종목) 기준.
    # 풀 = 같은 시장의 다른 보유·기초 종목 + 그 시장 지수(자기 종목은 빼서 이중 계산 방지)
    insts = {h["base"] or h["ticker"] for h in hold}
    is_kr = lambda t: t.endswith((".KS", ".KQ"))
    earns = earnings([(h["종목명"], (h.get("기초") or "").strip() or h["티커"].strip()) for h in hs])
    import numpy as np  # 실적까지 남은 '거래일' 수(주말 제외) — 20거래일 시나리오와 같은 단위
    earn_days = {e["name"]: int(np.busday_count(dt.date.today(), dt.date.fromisoformat(e["date"]))) for e in earns}
    for h in hold:
        inst = h["base"] or h["ticker"]
        T = h["base_tech"] if h["base"] else h["tech"]
        pool = sorted(p for p in insts if p != inst and is_kr(p) == is_kr(inst)) + ["^KQ11" if is_kr(inst) else "^IXIC"]
        cp = chart_prob(inst, T, pool)
        if cp:
            idx = "^KQ11" if h["ticker"].endswith((".KS", ".KQ")) else "^IXIC"
            ic = OHLCV["Close"][idx].dropna()
            mkt = (1 if ic.iloc[-1] > ic.iloc[-60:].mean() else -1) if len(ic) > 60 else 0
            rs = (T.get("rs") or 0)
            cp["inputs"] = {"mkt": round((mkt + (1 if rs > 0 else -1)) / 2),
                            "dilute": any(k.lower() in n["title"].lower() for n in h["news"] for k in DILUTE)
                                      or any(f["risky"] for f in (h.get("filings") or [])),  # 증자 관련 공시도 희석 요인
                            "earn_days": earn_days.get(h["name"])}
        h["prob"] = cp
        h["dir"] = direction(inst, pool) if data_ok(inst) else None
    costed =[h for h in hold if h["cost"]]
    val, cost = sum(h["won"] for h in costed), sum(h["cost"] for h in costed)
    # GPT 3차 ①③: 같은 기초 종목 묶음 비중·방향 노출(2배 ETF는 2배), 하루 급락 충격
    total_won = sum(h["won"] for h in hold) or 1
    groups = {}
    for h in hold:
        g = groups.setdefault(h["base"] or h["ticker"], {"inst": h["base"] or h["ticker"], "names": [], "weight": 0.0, "exposure": 0.0})
        g["names"].append(h["name"])
        g["weight"] += h["won"] / total_won * 100
        g["exposure"] += h["won"] * (2 if h["base"] else 1) / total_won * 100
    groups = sorted(({**g, "weight": round(g["weight"], 1), "exposure": round(g["exposure"], 1)} for g in groups.values()),
                    key=lambda g: -g["exposure"])
    shock = -round(sum(h["won"] * (0.2 if h["base"] else 0.1) for h in hold))
    # GPT 3차 ④: 2배 ETF 실제 누적 수익률 − 기초 종목 누적×2 (5·20거래일)
    for h in hold:
        if h["base"]:
            both = OHLCV["Close"][[h["ticker"], h["base"]]].dropna()  # 같은 날짜끼리 맞춘다(Codex 4차)
            ec, bc = both[h["ticker"]], both[h["base"]]
            h["lev_gap"] = {str(n): round(((ec.iloc[-1] / ec.iloc[-1 - n] - 1) - 2 * (bc.iloc[-1] / bc.iloc[-1 - n] - 1)) * 100, 1)
                            for n in (5, 20) if len(both) > n}
    fx_chg = q.get("KRW=X", (0, 0))[1]  # 해외 종목의 오늘 원화 손익엔 환율 변동도 들어간다
    prev = lambda h: h["won"] / (1 + (h["chg"] if h["fresh"] else 0) / 100) / (1 if h["ticker"].endswith((".KS", ".KQ")) else 1 + fx_chg / 100)
    acct = {"pnl": round((val / cost - 1) * 100, 1) if cost else None, "total": sum(h["won"] for h in hold),
            "today": round(sum(h["won"] - prev(h) for h in hold)), "fx_chg": round(fx_chg, 2), "fx": round(fx, 2),
            "fx_ok": "KRW=X" in q, "missing": missing, "groups": groups, "shock": shock,
            "gain": round(val - cost), "costed_n": len(costed), "hold_n": len(hold)}
    return {"id": bid(kind), "kind": kind, "at": dt.datetime.now().isoformat(timespec="minutes"),
            "earnings": earns,
            "market": [{"name": n, "value": round(q[MARKET[n]][0], 2), "chg": round(q[MARKET[n]][1], 2)}
                       for n in ORDER[kind] if MARKET[n] in q],
            "sector": sorted(({"name": n, "chg": round(q[t][1], 2)} for n, t in sectors.items() if t in q),
                             key=lambda s: -s["chg"]),
            "hold": hold, "acct": acct,
            "asof": {"kr": q.get("^KQ11", (0,) * 5)[4] if "^KQ11" in q else None,  # 시장별 마지막 봉 날짜(화면에 '국내 10/23 종가')
                     "us": q.get("^IXIC", (0,) * 5)[4] if "^IXIC" in q else None}}


# ---------- 관망 ----------

def headline(d):
    idx = "코스피" if d["kind"] == "pm" else "나스닥"
    m = {x["name"]: x["chg"] for x in d["market"]}.get(idx, 0)
    s = d["sector"]
    return f"{idx} {m:+.1f}%, {s[0]['name']} 강세가 이끈 하루" if m >= 0 else f"{idx} {m:+.1f}%, {s[-1]['name']} 약세가 두드러진 하루"


NOTE_FORMAT = ('JSON 하나만 출력: {"headline": "가장 중요한 한 가지를 결론으로, 25자 이내", "flow": "원인과 결과 2문장", '
               '"points": ["관망 포인트 2개, 각 40자 이내"], "pick": "관심 섹터 1~2개와 이유 한 줄", '
               '"review": "직전 브리핑 관망 포인트가 실제로 어떻게 됐는지 1문장, 틀렸으면 틀렸다고(직전이 없으면 빈 문자열)", '
               '"events": [{"time": "오늘 21:30", "title": "일정 이름", "why": "왜 중요한지 한 줄"}], '
               '"holdings": [{"name": "입력 hold의 name 그대로", "why": "오늘 움직인 이유 1줄(뉴스 근거, 근거가 약하면 끝에 \'(추정)\')", '
               '"watch": "다음에 지켜볼 것 1줄(일정·기초 종목·공시·지표)", "risk": "사실 기반 위험 1줄(희석·레버리지 손실·공매도 등)", '
               '"tone": "우호|중립|비우호 중 하나 — 뉴스 흐름의 방향", '
               '"simple": "주식 초보용 한 줄 차트 평가: 지금 오르는 흐름인지 내리는 흐름인지와 가장 큰 이유 하나(hold의 dir.state·dir.signals 근거, '
               '2배 ETF는 따라가는 주식). \'이 가격을 넘으면/깨지면\' 같은 조건문 금지. 전문용어·영문 약어 금지", '
               '"chart": "차트 해석 2문장: 추세(trend·이동평균), 모멘텀(RSI·MACD), 거래량, 지지·저항 위치", '
               '"scenario": {"up": "상승 시나리오: 어떤 가격·조건을 넘으면 어떤 흐름", "base": "기본 시나리오", '
               '"down": "하락 시나리오: 어떤 가격·조건이 깨지면 어떤 흐름"}}], '
               '"sources": [{"title": "근거 기사 제목", "url": "주소"}]}. '
               "holdings는 입력 hold의 모든 종목을 같은 순서로 쓴다. 이 브리핑의 중심이다. "
               "해외 가격은 반드시 '23.85달러'처럼 숫자 바로 뒤에 '달러'를 붙여 쓴다($ 기호 금지) — 화면·카톡에서 원화로 자동 환산된다. "
               "차트 해석과 시나리오는 hold의 tech(레버리지 ETF는 base_tech 먼저) 값만 근거로 쓰고 가격은 실제 값을 인용한다. "
               "독자는 주식 초보다: 모든 문장은 쉬운 말로 쓰고, 전문용어(ADX·RSI·ATR·OBV·스윙·무효화 등)는 본문에 쓰지 말거나 "
               "쉬운 말 뒤 괄호에만 넣는다(예: '하루에 흔들리는 폭(ATR)'). '고점·저점이 계속 낮아지는 중', '자주 막혔던 가격대', "
               "'평소의 절반 거래량', '이 아래로 마감하면 지금 분석은 틀린 것'처럼 쓴다. "
               "단정하지 않는다: '산 사람 대부분 손해' 대신 '최근 평균 가격보다 아래', 2배 ETF는 '하루 등락률의 약 2배를 목표로 하는 상품', "
               "'좋은 신호·조심 신호' 대신 '위쪽 기준·아래쪽 기준'. "
               "차트 해석 규칙: ①첫 문장은 국면(regime·ADX, struct 고점·저점 방향, trend 이평 배열) ②숫자는 뜻으로 번역, 나열 금지 "
               "③추세장에서 RSI 70 이상을 곧바로 과열로 쓰지 않음 ④div·squeeze·obv 괴리는 '경고/가능성'으로, 확인될 가격을 함께 "
               "⑤지지·저항은 구간(sup_lo~sup, res~res_hi)으로 가장 가까운 것만 ⑥거래량은 vol_x배 ⑦하루 예상 범위는 range_lo~range_hi(±1 ATR, atr_pct%) "
               "⑧52주 고점 대비(from_hi)와 지수 대비 상대강도(rs, 20일) 한 줄 ⑨단정 어미 금지. "
               "금요일 오후(pm)면 \"weekly\": {\"summary\": \"이번 주 내 종목 정리 2문장\", \"next_week\": \"다음 주 지켜볼 일정·가격 2개, 각 40자 이내\"}도 쓴다(data의 weekly 숫자 근거). "
               "hold의 filings(공시)·flows(외국인·기관 수급)·earn_react(지난 실적 발표 반응)가 있으면 why·watch·risk에 반영한다. "
               "시나리오 규칙: up = '종가 [res] 위 마감(거래량 1.5배 이상이면 신뢰↑) → 다음 구간 [res_hi 위 구간·+2ATR]', "
               "down = '종가 [sup_lo] 아래 마감 → 다음 지지 구간, 무효화 [invalid]', base = 'sup~res 박스, 이탈 전까지 방향성 약함'. "
               "트리거는 종가 기준, 레벨 간격이 1 ATR보다 좁으면 생략, 소형주·레버리지 ETF는 갭으로 레벨을 건너뛸 수 있다고 한 줄. "
               "레버리지 ETF는 기초 종목 가격으로만 쓰고 ETF 가격으로 바꿔 쓰지 않는다(2배 일간 리셋이라 여러 날 뒤 ETF 가격은 비례하지 않음). "
               "testing(지금 시험 중인 가격대)이 있으면 차트 해석에 먼저 쓴다. sup_n·res_n이 0이면 실제 가격대가 없어 흔들림 폭 2배로 잡은 경계라고 밝힌다. "
               "확률·목표가·매매 지시 금지. "
               "레버리지 ETF(base가 있는 종목)는 기초 종목(base, base_chg) 기준으로 설명하고, decay(한 달 변동성 손실 근사 %)가 크면 risk에 쓴다. "
               "hold의 news는 그 종목의 최근 기사 제목이다. events는 다음 브리핑 전까지의 경제지표 발표·중앙은행·보유 종목 실적(입력 earnings) 중 중요한 것 최대 3개. "
               "매수·매도·목표가 같은 매매 지시는 쓰지 말고 '~ 흐름', '~ 지켜볼 만함' 수준으로 쓴다. "
               "숫자는 입력 JSON에 있는 값만 쓰고, 뉴스에서 온 내용은 sources에 근거를 남긴다. 불확실하면 '확인 필요'. "
               "중요한 뉴스가 없으면 억지로 만들지 말고 '특이사항 없음'이라고 쓴다.")


def lint(note, d):
    """금지어와 입력에 없는 퍼센트 숫자를 찾는다. 빈 목록이면 통과."""
    text = " ".join([note.get("headline", ""), note.get("flow", ""), note.get("pick", ""), note.get("review", "")]
                    + note.get("points", []) + [e.get("title", "") + " " + e.get("why", "") for e in note.get("events", [])]
                    + [" ".join([v.get(k, "") for k in ("why", "watch", "risk", "simple", "chart")]
                                + list((v.get("scenario") or {}).values())) for v in note.get("holdings", [])])
    issues = [f"금지어 '{w}'" for w in BANNED if w in text.replace("공매도", "")]  # 공매도 비율은 사실 정보
    known = {f"{abs(x):.1f}" for x in [m["chg"] for m in d["market"]] + [s["chg"] for s in d["sector"]]
             + [v for h in d["hold"] for v in (h["chg"], h["pnl"], h.get("chg5"), h.get("base_chg"), h.get("decay"), h.get("short"))
                if v is not None] + [d["acct"]["pnl"] or 0]
             + [t[k] for h in d["hold"] for t in (h.get("tech"), h.get("base_tech")) if t
                for k in ("dist_ma20", "from_hi", "from_lo", "atr_pct", "rs") if t.get(k) is not None]}
    issues += [f"입력에 없는 숫자 {n}%" for n in re.findall(r"(\d+(?:\.\d+)?)\s*%", text)
               if f"{float(n):.1f}" not in known]
    return issues


def comment(d):
    """Claude API 키가 있으면 뉴스 검색까지 해서 쓰고(검사 실패 시 1번 재작성), 없으면 숫자로 만든 요약."""
    key = CFG.get("anthropic_api_key")
    if key:
        data = json.dumps({k: d[k] for k in ("market", "sector", "hold", "acct")}, ensure_ascii=False)
        msgs = [{"role": "user", "content":
                 f"오늘은 {dt.date.today()} ({'아침, 간밤 미국장 마감 후' if d['kind'] == 'am' else '오후, 국내장 마감 후'}).\n"
                 f"아래 지표와 오늘 주요 뉴스를 근거로 한국어 시황 브리핑을 써라.\n{data}\n\n{NOTE_FORMAT}"}]
        hdr = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        for _ in range(2):
            body = {"model": CFG.get("model", "claude-sonnet-5-5"), "max_tokens": 1500, "messages": msgs, "tools": tools}
            r = requests.post("https://api.anthropic.com/v1/messages", json=body, headers=hdr, timeout=120)
            if not r.ok:
                tools = []  # 검색 도구 실패 시 검색 없이 재시도
                continue
            text = "".join(c.get("text", "") for c in r.json()["content"] if c["type"] == "text")
            try:
                note = json.loads(text[text.index("{"):text.rindex("}") + 1])
            except ValueError:
                continue
            if not (bad := lint(note, d)):
                return note
            msgs += [{"role": "assistant", "content": text}, {"role": "user", "content": f"고칠 것: {bad}. 같은 형식으로 다시."}]
    s = d["sector"]
    return {"headline": headline(d),
            "flow": f"강한 섹터는 {s[0]['name']}({s[0]['chg']:+.1f}%), 약한 섹터는 {s[-1]['name']}({s[-1]['chg']:+.1f}%)입니다.",
            "points": ["뉴스 기반 관망은 Claude 예약 작업이나 API 키로 채워집니다"], "pick": "", "sources": []}


# ---------- 이미지 ----------

def chg(d, digits=2):
    c, a = ("up", "▲") if d > 0 else ("dn", "▼") if d < 0 else ("eq", "–")
    return f'<span class="{c}">{a} {abs(d):.{digits}f}%</span>'


CSS = """*{box-sizing:border-box;margin:0}
body{width:1080px;padding:56px;background:#eef1ea;color:#191f1c;font-family:'Pretendard Variable','Malgun Gothic',sans-serif;
 font-size:30px;line-height:1.5;word-break:keep-all;font-variant-numeric:tabular-nums}
.hd{display:flex;justify-content:space-between;font-size:26px;font-weight:500;color:#5c6660}
h1{font-size:46px;font-weight:700;line-height:1.3;letter-spacing:-.02em;margin:18px 0 36px}
.card{background:#fff;border-radius:28px;padding:36px 36px 32px;margin-bottom:24px}
.ct{font-size:26px;font-weight:600;color:#5c6660;margin-bottom:22px}
.up{color:#c62828}.dn{color:#1f56c9}.eq{color:#5c6660}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:30px 20px}
.grid .n{font-size:26px;color:#5c6660}.grid .v{font-size:36px;font-weight:600;line-height:1.25}.grid .c{font-size:26px;font-weight:600}
.bar{display:grid;grid-template-columns:150px 1fr 150px;align-items:center;height:60px}
.bar .v{text-align:right;font-weight:600}
.track{position:relative;height:22px}
.track:before{content:'';position:absolute;left:50%;top:-8px;bottom:-8px;border-left:2px solid #d5dad2}
.track i{position:absolute;top:0;bottom:0;border-radius:4px}
.txt .h{font-size:26px;font-weight:600;color:#3f6b3a;margin:20px 0 6px}.txt .h:first-child{margin-top:0}
.txt p{line-height:1.6}
.hero{background:#dfe8d5}.hero .big{font-size:112px;font-weight:700;letter-spacing:-.03em;line-height:1.1;margin:4px 0 14px}
.hero .sub{display:flex;gap:40px;color:#3d4640}.hero .sub b{font-weight:600}
.tb{display:grid;grid-template-columns:1fr 220px 170px 190px;align-items:center}
.tb.th{font-size:24px;color:#5c6660;padding-bottom:12px}
.tb.tr{padding:18px 0;border-top:2px solid #eef0ec}
.tb>*:not(:first-child){text-align:right}
.tb .nm{font-weight:600;line-height:1.3}.tb .q{font-size:24px;color:#5c6660}
.tb .p{font-weight:600}
.tb .vw{grid-column:1/-1;text-align:left!important;font-size:24px;line-height:1.55;color:#3d4640;margin-top:10px;
 padding:12px 16px;background:#f3f5f0;border-radius:14px}
.ft{font-size:24px;color:#5c6660;line-height:1.6;margin-top:28px}"""


def page(kind, title, inner):
    when = "미국장 마감 기준" if kind == "am" else "국내장 마감 기준"
    link = f" · 자세히: 마켓 브리핑 대시보드" if CFG.get("dashboard_url") else ""
    return (f'<!doctype html><meta charset="utf-8"><style>{CSS}</style>'
            f'<div class="hd"><span>{"아침" if kind == "am" else "오후"} 브리핑 · {when}</span>'
            f'<span>{dt.datetime.now():%Y.%m.%d %H:%M}</span></div><h1>{html.escape(title)}</h1>{inner}'
            f'<div class="ft">시세: Yahoo Finance{link} · 시장 해설이며 매매 권유가 아닙니다.</div>')


_NUM = r"(\d[\d,]*(?:\.\d+)?)"


def krw_text(s, fx):
    """문장 속 '23.85달러'·'22.16~23.47달러'를 원화로(사용자 요청: 모든 가격은 원 표기). 홈페이지 template의 krwText와 같은 규칙."""
    won = lambda x: f"{float(x.replace(',', '')) * fx:,.0f}"
    s = re.sub(_NUM + r"(\s*[~∼–-]\s*)" + _NUM + r"\s*달러", lambda m: f"{won(m[1])}{m[2]}{won(m[3])}원", str(s))
    return re.sub(_NUM + r"\s*달러", lambda m: f"{won(m[1])}원", s)


def market_html(d, note):
    cells = "".join(f'<div><div class="n">{m["name"]}</div><div class="v">{m["value"]:,.2f}</div>'
                    f'<div class="c">{chg(m["chg"])}</div></div>' for m in d["market"])
    mx = max([abs(s["chg"]) for s in d["sector"]] + [0.5])
    bars = "".join(
        f'<div class="bar"><span>{s["name"]}</span><div class="track"><i style="{"left:50%" if s["chg"] >= 0 else "right:50%"};'
        f'width:{abs(s["chg"]) / mx * 48:.1f}%;background:{"#e58b84" if s["chg"] >= 0 else "#8aa8e6"}"></i></div>'
        f'<span class="v">{chg(s["chg"], 1)}</span></div>' for s in d["sector"])
    e = lambda s: html.escape(krw_text(s, d["acct"].get("fx") or 1400))
    body = f'<div class="h">흐름</div><p>{e(note.get("flow", ""))}</p>'
    if note.get("points"):
        body += '<div class="h">관망 포인트</div>' + "".join(f"<p>- {e(p)}</p>" for p in note["points"])
    if note.get("pick"):
        body += f'<div class="h">관심 섹터</div><p>{e(note["pick"])}</p>'
    evs = [(x.get("time", ""), x.get("title", "")) for x in note.get("events", [])][:3] \
        + [(x["date"][5:].replace("-", "/"), f'{x["name"]} 실적 발표') for x in d.get("earnings", [])][:2]
    if evs:
        body += '<div class="h">다가오는 일정</div>' + "".join(f"<p>{e(t)} · {e(s)}</p>" for t, s in evs)
    if note.get("review"):
        body += f'<div class="h">지난 관망 점검</div><p>{e(note["review"])}</p>'
    return page(d["kind"], note.get("headline") or headline(d),
                f'<div class="card"><div class="ct">시장 지표</div><div class="grid">{cells}</div></div>'
                f'<div class="card"><div class="ct">{"미국" if d["kind"] == "am" else "국내"} 섹터 등락</div>{bars}</div>'
                f'<div class="card txt"><div class="ct">오늘의 관망</div>{body}</div>')


def account_html(d, note):
    a = d["acct"]
    pnl = a["pnl"] or 0
    sign = lambda x: f"{'+' if x >= 0 else '−'}{abs(x):,.0f}원"
    views = {v.get("name"): v for v in note.get("holdings", [])}
    fx = d["acct"].get("fx") or 1400
    e = lambda s: html.escape(krw_text(s, fx))

    def price(h):  # 1주당 현재가(해외도 원화로 환산)
        p = (h.get("tech") or {}).get("price")
        return "–" if p is None else f"{p if h.get('ticker', '').endswith(('.KS', '.KQ')) else p * fx:,.0f}원"

    def view(h):
        v = views.get(h["name"])
        base = f'기초 {h["base"]} {h["base_chg"]:+.2f}%' if h.get("base_chg") is not None else ""
        lines = [e(v.get("why", "")) if v else base]  # 관망이 있으면 기초 종목 설명은 관망 문장에 들어 있다
        if v and v.get("watch"):
            lines.append(f"지켜볼 것 · {e(v['watch'])}")
        lines = [l for l in lines if l]
        return f'<div class="vw">{"<br>".join(lines)}</div>' if lines else ""

    rows = "".join(
        f'<div class="tb tr"><div><div class="nm">{e(h["name"])}</div><div class="q">{h["qty"]:g}주 · 현재가 {price(h)}</div></div>'
        f'<span>{h["won"]:,.0f}원</span><span>{chg(h["chg"])}</span>'
        f'<span class="p">{chg(h["pnl"], 1) if h["pnl"] is not None else "–"}</span>{view(h)}</div>' for h in d["hold"])
    hero = (f'<div class="card hero"><div class="ct">평가손익 (원금 입력 종목 기준)</div>'
            f'<div class="big {"up" if pnl >= 0 else "dn"}">{"+" if pnl >= 0 else "−"}{abs(pnl):.1f}%</div>'
            f'<div class="sub"><span>평가금 <b>{a["total"]:,.0f}원</b></span><span>오늘 <b class="{"up" if a["today"] >= 0 else "dn"}">'
            f'{sign(a["today"])}</b></span></div></div>')
    table = (f'<div class="card"><div class="ct">보유 종목 {len(d["hold"])}개</div>'
             f'<div class="tb th"><span>종목 · 수량</span><span>평가금</span><span>오늘</span><span>손익</span></div>{rows}</div>')
    return page(d["kind"], f"내 계좌 {'+' if pnl >= 0 else '−'}{abs(pnl):.1f}%, 오늘 {sign(a['today'])}", hero + table)


def dir_line(h):
    """카톡 한 줄: 차트 판정(흐름 + 일주일 뒤 오를 확률). 2배 ETF는 따라가는 주식 기준."""
    v = h.get("dir")
    if not v or "5" not in v["p"]:
        return ""
    return f"{h['base'] + ' ' if h.get('base') else ''}{v['state']} · 일주일 뒤 오를 확률 {v['p']['5']['up']}%"


def summary_html(d, note):
    """카톡 첫 장: 종목마다 현재가·오늘 변동폭 + 한 줄 전망. 홈페이지와 같은 다크 톤."""
    e = html.escape
    views = {v.get("name"): v for v in note.get("holdings", [])}
    rows = ""
    for h in d["hold"]:
        kr = h["ticker"].endswith((".KS", ".KQ"))
        px = (h.get("tech") or {}).get("price")
        if px is None:
            continue
        diff = px * h["chg"] / (100 + h["chg"])  # 1주당 오늘 움직인 가격
        cls = "up" if h["chg"] > 0 else "dn" if h["chg"] < 0 else "eq"
        a = "▲" if h["chg"] > 0 else "▼" if h["chg"] < 0 else "–"
        fmt = lambda v: f"{v * (1 if kr else d['acct'].get('fx') or 1400):,.0f}원"  # 해외도 원화로(브리핑 때 환율)
        line = dir_line(h) or views.get(h["name"], {}).get("line", "")
        rows += (f'<div class="r"><div class="l"><b>{e(h["name"])}</b><span>{e(line)}</span></div>'
                 f'<div class="p"><b>{fmt(px)}</b><span class="{cls}">{a} {abs(h["chg"]):.2f}% · {fmt(abs(diff))}</span></div></div>')
    a = d["acct"]
    when = f"{d['id'][5:7].lstrip('0')}.{d['id'][8:10].lstrip('0')} {'아침' if d['kind'] == 'am' else '오후'}"
    css = """*{box-sizing:border-box;margin:0}body{width:1080px;padding:56px;background:#0e0f0d;color:#f2f4ee;
     font-family:'Pretendard Variable','Malgun Gothic',sans-serif;font-variant-numeric:tabular-nums;word-break:keep-all}
     .hd{display:flex;justify-content:space-between;align-items:center;color:#a2a89c;font-size:28px}
     .hd b{color:#f2f4ee;font-size:40px;letter-spacing:-.03em}.dot{display:inline-block;width:18px;height:18px;border-radius:50%;background:#cdf54a;margin-right:14px}
     .acct{margin:28px 0 20px;display:flex;gap:16px}.acct div{flex:1;background:#181a17;border-radius:24px;padding:22px 26px;font-size:24px;color:#a2a89c}
     .acct b{display:block;font-size:38px;color:#f2f4ee;margin-top:4px}.acct b.up{color:#ff6161}.acct b.dn{color:#6a9cff}
     .r{display:flex;justify-content:space-between;gap:24px;align-items:center;background:#181a17;border-radius:24px;padding:26px 30px;margin-top:12px}
     .l{min-width:0}.l b{font-size:34px;letter-spacing:-.02em;display:block}.l span{font-size:24px;color:#a2a89c;display:block;margin-top:6px;line-height:1.35}
     .p{text-align:right;flex:none}.p b{font-size:36px;display:block}.p span{font-size:26px;font-weight:700;display:block;margin-top:6px}
     .up{color:#ff6161}.dn{color:#6a9cff}.eq{color:#a2a89c}.ft{color:#747a70;font-size:22px;margin-top:22px}"""
    sign = lambda x: f"{'+' if x > 0 else '−' if x < 0 else ''}{abs(x):,.0f}원"
    return (f'<!doctype html><meta charset="utf-8"><style>{css}</style>'
            f'<div class="hd"><b><i class="dot"></i>오늘 내 종목</b><span>{when} · {"미국장" if d["kind"] == "am" else "국내장"} 마감 기준</span></div>'
            f'<div class="acct"><div>평가손익<b class="{"up" if (a["pnl"] or 0) >= 0 else "dn"}">{"+" if (a["pnl"] or 0) >= 0 else "−"}{abs(a["pnl"] or 0):.1f}%</b></div>'
            f'<div>오늘<b class="{"up" if a["today"] >= 0 else "dn"}">{sign(a["today"])}</b></div></div>{rows}'
            f'<p class="ft">현재가 옆 숫자 = 오늘 등락률 · 1주당 움직인 가격. 확률 = 같은 차트 상태였던 과거에 일주일 뒤 올라 있던 비율'
            f'(과거 검증 적중률 약 {DIR_CHECK["hit5"]:.0f}%, 참고용). 매매 권유가 아닙니다.</p>')


def shoot(doc, path, bg=BG):
    """Edge 헤드리스로 캡처 후 아래 빈 배경(bg)을 잘라낸다."""
    src = path.with_suffix(".html")
    src.write_text(doc, "utf-8")
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    "--window-size=1080,3200", "--virtual-time-budget=5000", f"--screenshot={path}", src.as_uri()],
                   check=True, capture_output=True, timeout=60)
    im = Image.open(path).convert("RGB")
    y = im.height - 1
    while y > 0 and all(lo == hi == c for (lo, hi), c in zip(im.crop((0, y, im.width, y + 1)).getextrema(), bg)):
        y -= 1
    im.crop((0, 0, im.width, min(im.height, y + 56))).save(path)


# ---------- 대시보드 기록 ----------

def rows(d, note):
    """대시보드 데이터셋 'brief'의 행. key = 브리핑id|구역|이름 — 대시보드가 이 키로 값을 찾는다."""
    out = []

    def add(section, name, **kw):
        out.append({"key": f"{d['id']}|{section}|{name}", "id": d["id"], "kind": d["kind"], "at": d["at"],
                    "section": section, "name": name, "value": None, "chg": None, "qty": None, "won": None,
                    "pnl": None, "weight": None, "base": None, "base_chg": None, "decay": None, "short": None,
                    "text": None, "sub": None, "detail": None, "tone": None, "url": None, "ord": len(out)} | kw)

    for m in d["market"]:
        add("market", m["name"], value=m["value"], chg=m["chg"])
    for s in d["sector"]:
        add("sector", s["name"], chg=s["chg"])
    total = d["acct"]["total"] or 1
    for h in d["hold"]:
        add("hold", h["name"], qty=h["qty"], won=h["won"], chg=h["chg"], pnl=h["pnl"], weight=round(h["won"] / total * 100, 1),
            base=h.get("base"), base_chg=h.get("base_chg"), decay=h.get("decay"), short=h.get("short"))
    for v in note.get("holdings", []):
        add("view", v.get("name", ""), text=v.get("why", ""), sub=v.get("watch", ""), detail=v.get("risk", ""), tone=v.get("tone"))
    costed = [h for h in d["hold"] if h["cost"]]
    cost = round(sum(h["cost"] for h in costed))
    add("acct", "pnl", pnl=d["acct"]["pnl"])
    add("acct", "total", won=d["acct"]["total"])
    add("acct", "today", won=d["acct"]["today"])
    add("acct", "cost", won=cost)  # 원금 입력 종목만
    add("acct", "gain", won=round(sum(h["won"] for h in costed)) - cost)
    add("note", "headline", text=note.get("headline") or headline(d))
    add("note", "flow", text=note.get("flow", ""))
    for i, p in enumerate(note.get("points", [])):
        add("note", f"point{i + 1}", text=p)
    if note.get("pick"):
        add("note", "pick", text=note["pick"])
    if note.get("review"):
        add("note", "review", text=note["review"])
    for i, e in enumerate(note.get("events", [])):
        add("event", f"ev{i + 1}", text=e.get("title", ""), sub=e.get("time", ""), detail=e.get("why", ""))
    for e in d.get("earnings", []):
        add("event", f"실적:{e['name']}", text=f"{e['name']} 실적 발표", sub=e["date"], detail="보유 종목")
    for i, s in enumerate(note.get("sources", [])):
        add("note", f"source{i + 1}", text=s.get("title", ""), url=s.get("url"))
    return out


ARCH, SITE = HERE / "data" / "archive.json", HERE / "site"


def weekly(d, force=False):
    """금요일 오후 브리핑(또는 force)이면 이번 주(월~) 계좌·종목 변화를 정리한다. 아니면 None."""
    if not force and not (d["kind"] == "pm" and dt.date.fromisoformat(d["id"][:10]).weekday() == 4):
        return None
    arch = json.loads(ARCH.read_text("utf-8")) if ARCH.exists() else {}
    mon = dt.date.fromisoformat(d["id"][:10])
    mon = (mon - dt.timedelta(days=mon.weekday())).isoformat()
    week = sorted(k for k in arch if k >= mon and k < d["id"])
    first = arch[week[0]]["d"]["acct"] if week else None
    return {"from": week[0] if week else None, "pnl_from": first and first["pnl"], "pnl_to": d["acct"]["pnl"],
            "total_from": first and first["total"], "total_to": d["acct"]["total"],
            "holds": sorted(({"name": h["name"], "chg5": h.get("chg5")} for h in d["hold"]), key=lambda x: -(x["chg5"] or 0)),
            "earnings_next": [e for e in d.get("earnings", []) if e["date"] <= (dt.date.today() + dt.timedelta(days=9)).isoformat()]}


def delta(a, b):
    """직전 브리핑(a) → 지금(b)에서 초보가 알아야 할 변화만 쉬운 말로."""
    out = []
    ta, tb = (a.get("base_tech") or a.get("tech") or {}), (b.get("base_tech") or b.get("tech") or {})
    flow = lambda t: "" if t.get("adx") is None else ("하락세" if t["adx"] > 25 and "하락" in t.get("regime", "") else
                                                      "상승세" if t["adx"] > 25 else "박스권")
    if flow(ta) and flow(tb) and flow(ta) != flow(tb):
        out.append(f"흐름 {flow(ta)} → {flow(tb)}")
    sa, sb = (a.get("dir") or {}).get("state"), (b.get("dir") or {}).get("state")
    if sa and sb and sa != sb:
        out.append(f"차트 판정 {sa} → {sb}")
    pa, pb = a.get("prob") or {}, b.get("prob") or {}
    # 해외 가격은 '달러'를 붙여 둔다 → 홈페이지가 원화로 바꿔 보여 줌
    unit = (lambda v: f"{v:,.0f}원") if (pb.get("inst") or "").endswith((".KS", ".KQ")) else (lambda v: f"{v:g}달러")
    for k, label in (("up", "위쪽 기준"), ("dn", "아래쪽 기준")):
        va, vb = (pa.get("levels") or {}).get(k), (pb.get("levels") or {}).get(k)
        if va and vb and abs(vb / va - 1) > 0.01:
            out.append(f"{label} {unit(va)} → {unit(vb)}")
    ha, hb = ((pa.get("h") or {}).get("20") or {}), ((pb.get("h") or {}).get("20") or {})
    if ha and hb and abs(hb["up"] - ha["up"]) >= 5:
        out.append(f"한 달 '좋은 신호 먼저' {ha['up']}% → {hb['up']}%")
    if (ta.get("testing") is None) != (tb.get("testing") is None):
        out.append("시험 중인 가격대에 들어옴" if tb.get("testing") else "시험 중이던 가격대를 벗어남")
    return out


def record(d, note):
    HIST.parent.mkdir(exist_ok=True)
    old = json.loads(HIST.read_text("utf-8")) if HIST.exists() else []
    old = [r for r in old if r["id"] != d["id"]] + rows(d, note)
    ids = sorted({r["id"] for r in old})[-KEEP:]
    HIST.write_text(json.dumps([r for r in old if r["id"] in ids], ensure_ascii=False), "utf-8")
    # 홈페이지용 기록: 브리핑 id → 숫자+노트 (뉴스 원문 목록은 빼서 가볍게)
    arch = json.loads(ARCH.read_text("utf-8")) if ARCH.exists() else {}
    prev_ids = sorted(k for k in arch if k < d["id"])
    if prev_ids:  # GPT 3차 ②: 직전 브리핑 대비 달라진 점
        prev = {h["name"]: h for h in arch[prev_ids[-1]]["d"]["hold"]}
        for h in d["hold"]:
            if h["name"] in prev:
                h["delta"] = delta(prev[h["name"]], h)
    arch[d["id"]] = {"d": d | {"hold": [{k: v for k, v in h.items() if k != "news"} for h in d["hold"]]}, "note": note}
    arch = {k: arch[k] for k in sorted(arch)[-KEEP:]}
    for k, b in arch.items():  # 캔들은 최신 브리핑에만 남겨 파일을 가볍게
        if k != d["id"]:
            for h in b["d"]["hold"]:
                (h.get("tech") or {}).pop("ohlc", None)
                h.pop("tf", None)
    ARCH.write_text(json.dumps(arch, ensure_ascii=False), "utf-8")
    site(arch)


def encrypt(text, password, iters=310_000):
    """AES-256-GCM, 키는 PBKDF2-SHA256. 브라우저 WebCrypto로 같은 방식으로 푼다."""
    import base64, os
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iters).derive(password.encode())
    b = lambda x: base64.b64encode(x).decode()
    return {"__enc": {"salt": b(salt), "iv": b(iv), "iter": iters, "ct": b(AESGCM(key).encrypt(iv, text.encode(), None))}}


def decrypt(obj, password):
    """encrypt()의 반대(클라우드에서 암호화된 보유 목록을 풀 때)."""
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    e = {k: base64.b64decode(v) if k != "iter" else v for k, v in obj["__enc"].items()}
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=e["salt"], iterations=e["iter"]).derive(password.encode())
    return AESGCM(key).decrypt(e["iv"], e["ct"], None).decode()


def site(arch):
    """site/template.html 에 기록을 넣어 site/index.html 을 만든다. site_password가 있으면 암호화해서 넣는다."""
    tpl = (SITE / "template.html").read_text("utf-8")
    data = json.dumps(arch, ensure_ascii=False)
    if CFG.get("site_password"):
        data = json.dumps(encrypt(data, CFG["site_password"]))
    (SITE / "index.html").write_text(tpl.replace("/*DATA*/{}", data.replace("</", "<\\/")), "utf-8")


def publish():
    """site/index.html 을 GitHub Pages 저장소(pages 폴더)에 올린다. 암호화 안 된 파일은 올리지 않는다."""
    import shutil
    repo, page = HERE / "pages", SITE / "index.html"
    if not (repo / ".git").exists():
        sys.exit("pages 폴더에 GitHub 저장소가 없습니다(git clone 필요)")
    if '"__enc"' not in page.read_text("utf-8"):
        sys.exit("암호화되지 않은 페이지라 올리지 않습니다 — config.json의 site_password를 넣고 build를 다시 하세요")
    shutil.copy(page, repo / "index.html")
    (repo / ".nojekyll").touch()
    extra = [f for f in ("manifest.webmanifest", "icon-180.png", "icon-192.png", "icon-512.png") if (SITE / f).exists()]
    for f in extra:  # 홈 화면 설치용(종목 정보 없음)
        shutil.copy(SITE / f, repo / f)
    # 클라우드 실시간 갱신용: 코드 사본 + 암호화한 보유 목록(종목명·수량은 암호문으로만 올라감)
    (repo / "cloud").mkdir(exist_ok=True)
    shutil.copy(HERE / "brief.py", repo / "cloud" / "brief.py")
    (repo / "cloud" / "holdings.enc.json").write_text(
        json.dumps(encrypt((HERE / "holdings.csv").read_text("utf-8-sig"), CFG["site_password"])), "utf-8")
    cloud = ["cloud/brief.py", "cloud/holdings.enc.json"]
    _push(repo, ["index.html", ".nojekyll", *extra, *cloud], f"브리핑 {dt.datetime.now():%Y-%m-%d %H:%M}")
    print("게시:", CFG.get("site_url") or repo)


def _push(repo, files, msg, squash_live=False):
    """git add/commit/push. 창 없이(pythonw로 작업 스케줄러에서 돌 때 콘솔이 번쩍이지 않게).
    squash_live: 직전 커밋도 실시간 갱신이면 덮어써서(amend + 강제 push) 10분마다 기록이 쌓이지 않게 한다."""
    import os, time
    nw = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, **nw)
    lock = repo.parent / "out" / "publish.lock"  # 브리핑 게시와 실시간 갱신이 동시에 git을 만지지 않게
    if lock.exists() and time.time() - lock.stat().st_mtime > 600:
        lock.unlink()  # 10분 넘은 잠금은 죽은 것
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return print("다른 게시가 진행 중이라 이번엔 건너뜀")
    try:
        git("add", *files)
        if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"], **nw).returncode:
            if squash_live and git("log", "-1", "--format=%s").stdout.startswith("실시간") \
                    and not git("status", "-sb").stdout.split("\n")[0].count("ahead"):
                git("commit", "--amend", "-m", msg)
                git("push", "--force-with-lease", "origin", "HEAD")
                return
            git("commit", "-m", msg)
        git("push", "origin", "HEAD")  # 바뀐 게 없어도: 전에 못 올라간 커밋이 있으면 이번에 올린다
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


def market_open(now=None, ext=False):
    """국내(평일 09:00~15:40) 또는 미국(한국 시각 22:20~다음 날 06:10, 서머타임 양쪽 포함) 장중인지.
    ext=True: 미국 프리마켓(17:00~)·애프터마켓(~10:10)까지 — 실시간 갱신용."""
    now = now or dt.datetime.now()
    t, wd = now.time(), now.weekday()
    kr = wd < 5 and dt.time(9, 0) <= t <= dt.time(15, 40)
    start, end = (dt.time(17, 0), dt.time(10, 10)) if ext else (dt.time(22, 20), dt.time(6, 10))
    us = (wd < 5 and t >= start) or (0 < wd <= 5 and t <= end)
    return kr or us


def live(force=False):
    """장중 10분마다(작업 스케줄러): 보유 종목 분봉·현재가만 새로 받아 암호화한 live.json으로 올린다. 노트·확률은 그대로."""
    import pandas as pd
    if not (force or market_open(ext=True)):
        return
    p = HERE / "holdings.csv"
    if not p.exists():  # 클라우드: 암호화된 보유 목록을 풀어서 쓴다(파일로 남기지 않음)
        rows = decrypt(json.loads((HERE / "holdings.enc.json").read_text("utf-8")), CFG["site_password"]).splitlines()
    else:
        rows = p.read_text("utf-8-sig").splitlines()
    hs = [r for r in csv.DictReader(rows) if r.get("티커", "").strip()]
    ts = {h["티커"].strip() for h in hs}
    fetch_intraday(ts)
    daily = yf.download(sorted(ts), period="7d", interval="1d", progress=False, auto_adjust=False, threads=False)["Close"]
    out = {}
    for h in hs:
        t = h["티커"].strip()
        m5 = INTRA_DF.get("5m")
        s = m5["Close"][t].dropna() if m5 is not None and t in m5["Close"] else pd.Series(dtype=float)
        dd = daily[t].dropna() if t in daily else pd.Series(dtype=float)
        if s.empty or len(dd) < 2:
            continue
        # 여러 거래소 종목을 한 번에 받으면 분봉 시간대가 뉴욕으로 맞춰진다 → 종목의 거래소 시간대로 세션 날짜를 정한다(Codex 4차)
        ts = s.index[-1]
        local = ts.tz_convert("Asia/Seoul" if t.endswith((".KS", ".KQ")) else "America/New_York") if ts.tzinfo else ts
        before = dd[[i.date() < local.date() for i in dd.index]]
        if before.empty:
            continue
        prev = before.iloc[-1]  # 세션 날짜보다 앞선 마지막 일봉 = 전일 종가
        kst = ts.tz_convert("Asia/Seoul") if ts.tzinfo else ts
        px, at, sess = float(s.iloc[-1]), kst.strftime("%m-%d %H:%M"), "정규장"
        if not t.endswith((".KS", ".KQ")) and (qt := quote(t, ext=True)):  # 미국: 공식 시세(프리·애프터마켓 포함, 마지막 체결)
            px, prev, at, sess = qt[0], qt[1], dt.datetime.fromtimestamp(qt[2]).strftime("%m-%d %H:%M"), qt[3]
        base = (h.get("기초") or "").strip()
        bq = quote(base, ext=True) if base else None  # 2배 ETF가 따라가는 주식의 지금 가격
        out[h["종목명"]] = {"price": round(px, 0 if px >= 1000 else 2 if px >= 1 else 4), "chg": round((px / prev - 1) * 100, 2),
                           "at": at, "session": sess, "tf": {iv: tech_tf(t, iv) for iv in INTRA},
                           "base_price": round(bq[0], 2) if bq else None, "base_chg": round((bq[0] / bq[1] - 1) * 100, 2) if bq else None}
    fq = quote("KRW=X")  # 홈페이지 원화 환산용 지금 환율
    body = json.dumps({"at": dt.datetime.now().isoformat(timespec="minutes"), "holds": out, "fx": round(fq[0], 2) if fq else None},
                      ensure_ascii=False)
    if not CFG.get("site_password"):
        sys.exit("site_password가 없어 실시간 파일을 올리지 않습니다")
    if os.environ.get("LIVE_OUT"):  # 클라우드: 파일만 쓰고 올리기는 워크플로가 live 브랜치로
        out_dir = pathlib.Path(os.environ["LIVE_OUT"])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "live.json").write_text(json.dumps(encrypt(body, CFG["site_password"])), "utf-8")
        return print("실시간 갱신:", len(out), "종목")
    repo = HERE / "pages"
    (repo / "live.json").write_text(json.dumps(encrypt(body, CFG["site_password"])), "utf-8")
    _push(repo, ["live.json"], f"실시간 {dt.datetime.now():%m-%d %H:%M}", squash_live=True)
    print("실시간 갱신:", len(out), "종목")


# ---------- 카카오 ----------

def kakao_token():
    t = json.loads(TOKEN.read_text("utf-8"))
    data = {"grant_type": "refresh_token", "client_id": CFG["kakao_rest_key"], "refresh_token": t["refresh_token"]}
    if CFG.get("kakao_client_secret"):
        data["client_secret"] = CFG["kakao_client_secret"]
    r = requests.post("https://kauth.kakao.com/oauth/token", data=data, timeout=30)
    r.raise_for_status()
    t.update(r.json())  # refresh_token은 만료 1달 전에만 새로 내려온다
    TOKEN.write_text(json.dumps(t), "utf-8")
    return t["access_token"]


def send(png, title, desc):
    at = kakao_token()
    with open(png, "rb") as f:
        up = requests.post("https://kapi.kakao.com/v2/api/talk/message/image/upload",
                           headers={"Authorization": f"KakaoAK {CFG['kakao_rest_key']}"}, files={"file": f}, timeout=60)
    up.raise_for_status()
    img = up.json()["infos"]["original"]
    url = CFG.get("site_url") or CFG.get("dashboard_url") or "https://m.stock.naver.com"
    link = {"web_url": url, "mobile_web_url": url}
    tpl = {"object_type": "feed", "content": {"title": title, "description": desc[:200], "image_url": img["url"],
           "image_width": img["width"], "image_height": img["height"], "link": link},
           "buttons": [{"title": "차트 분석 보기", "link": link}]}
    r = requests.post("https://kapi.kakao.com/v2/api/talk/memo/default/send", headers={"Authorization": f"Bearer {at}"},
                      data={"template_object": json.dumps(tpl, ensure_ascii=False)}, timeout=30)
    r.raise_for_status()


def auth(code):
    data = {"grant_type": "authorization_code", "client_id": CFG["kakao_rest_key"], "redirect_uri": REDIRECT, "code": code}
    if CFG.get("kakao_client_secret"):
        data["client_secret"] = CFG["kakao_client_secret"]
    r = requests.post("https://kauth.kakao.com/oauth/token", data=data, timeout=30)
    if not r.ok:
        sys.exit(f"토큰 발급 실패: {r.text}")
    TOKEN.write_text(r.text, "utf-8")
    print("토큰 저장 완료")


# ---------- 실행 ----------

def build(kind, note_path=None):
    d = json.loads((OUT / f"{bid(kind)}-data.json").read_text("utf-8"))
    note = json.loads(pathlib.Path(note_path).read_text("utf-8")) if note_path else comment(d)
    if bad := lint(note, d):
        sys.exit(f"관망 검사 실패라 만들지 않습니다(게시 중단): {bad}")
    tones = {v.get("name"): v.get("tone") for v in note.get("holdings", [])}
    log = []
    for h in d["hold"]:
        if cp := h.get("prob"):
            cp["outlook"] = {str(n): outlook_prob(cp, tones.get(h["name"]), cp.get("inputs", {}), n) for n in (5, 20)}
            log.append({"id": d["id"], "name": h["name"], "inst": cp["inst"], "price": (h["base_tech"] or h["tech"])["price"],
                        "levels": cp["levels"], "chart": {k: [v["up"], v["base"], v["dn"]] for k, v in cp["h"].items()},
                        "outlook": {k: [v["up"], v["base"], v["dn"]] for k, v in cp["outlook"].items()},
                        **({"dir": {k: v["up"] for k, v in h["dir"]["p"].items()}} if h.get("dir") else {})})
    sf = HERE / "data" / "scores.json"  # 채점 결과를 홈페이지에 보이게
    if sf.exists():
        sj = json.loads(sf.read_text("utf-8"))
        d["scores"] = {**sj["summary"], "pending": sj.get("pending", []), "items5": sj["items"]["5"]}
    else:
        d["scores"] = None
    d["weekly"] = weekly(d)  # 금요일 오후 브리핑이면 한 주 정리
    pf = HERE / "data" / "predictions.jsonl"  # 채점용 예측 기록(score로 채점). 같은 브리핑을 다시 만들면 교체
    old = [x for x in (pf.read_text("utf-8").splitlines() if pf.exists() else []) if x.strip() and json.loads(x)["id"] != d["id"]]
    pf.write_text("".join(x + "\n" for x in old) + "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in log), "utf-8")
    pngs = [(OUT / f"{d['id']}-1market.png", market_html(d, note))]
    if d["hold"]:
        pngs.append((OUT / f"{d['id']}-2account.png", account_html(d, note)))
    for png, doc in pngs:
        shoot(doc, png)
        print(png)
    if d["hold"]:  # 카톡 첫 장: 현재가·변동폭 한눈에(다크 배경이라 자르기 기준색이 다름)
        shoot(summary_html(d, note), OUT / f"{d['id']}-0summary.png", bg=(14, 15, 13))
    (OUT / f"{d['id']}-note.json").write_text(json.dumps(note, ensure_ascii=False), "utf-8")
    record(d, note)
    print(HIST)


def send_text(text):
    """카톡 글 메시지(200자 이내) + 홈페이지 버튼."""
    url = CFG.get("site_url") or "https://m.stock.naver.com"
    tpl = {"object_type": "text", "text": text[:200], "link": {"web_url": url, "mobile_web_url": url}, "button_title": "사이트에서 자세히"}
    r = requests.post("https://kapi.kakao.com/v2/api/talk/memo/default/send", headers={"Authorization": f"Bearer {kakao_token()}"},
                      data={"template_object": json.dumps(tpl, ensure_ascii=False)}, timeout=30)
    r.raise_for_status()


def lines_text(kind, d, note):
    """종목별 한 줄 전망을 200자 단위 메시지로 나눈다. line이 없으면 기억할 가격으로 만든다."""
    views = {v.get("name"): v for v in note.get("holdings", [])}
    rows = []
    for h in d["hold"]:
        v = views.get(h["name"], {})
        a = "▲" if h["chg"] > 0 else "▼" if h["chg"] < 0 else "–"
        line = dir_line(h) or v.get("line")
        if not line and h.get("prob"):
            kr = (h["base"] or h["ticker"]).endswith((".KS", ".KQ"))
            up = h["prob"]["levels"]["up"]
            line = "위쪽 기준 " + (f"{h['base']} " if h["base"] else "") + f"{up * (1 if kr else d['acct'].get('fx') or 1400):,.0f}원"
        rows.append(f"{h['name']} {a}{abs(h['chg']):.1f}% {line or ''}".strip())
    head = f"[{'아침' if kind == 'am' else '오후'} 내 종목 한 줄 전망]\n"
    msgs, cur = [], head
    for r in rows:
        if len(cur) + len(r) + 1 > 195:
            msgs.append(cur.rstrip())
            cur = ""
        cur += r + "\n"
    return msgs + [cur.rstrip()] if cur.strip() else msgs


def send_all(kind):
    note = json.loads((OUT / f"{bid(kind)}-note.json").read_text("utf-8"))
    d = json.loads((OUT / f"{bid(kind)}-data.json").read_text("utf-8"))
    when = "아침" if kind == "am" else "오후"
    if (p := OUT / f"{bid(kind)}-0summary.png").exists():  # 1) 현재가·변동폭·한 줄 전망 사진 한 장 + 홈페이지 버튼
        send(p, f"{when} 오늘 내 종목", note.get("headline", ""))
    else:
        for m in lines_text(kind, d, note):
            send_text(m)
    if (p := OUT / f"{bid(kind)}-2account.png").exists():  # 내 종목 관망이 중심이라 먼저 보낸다
        send(p, f"{when} 내 종목 관망", "종목별 움직인 이유와 지켜볼 것")
    send(OUT / f"{bid(kind)}-1market.png", f"{when} 시장 브리핑", note.get("headline", ""))


def fetch_save(kind):
    OUT.mkdir(exist_ok=True)
    d = collect(kind)
    p = OUT / f"{d['id']}-data.json"
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    print(p)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "auth":
        auth(a[1])
    elif a[0] == "fetch":
        fetch_save(a[1])
    elif a[0] == "build":
        build(a[1], a[2] if len(a) > 2 else None)
    elif a[0] == "send":
        send_all(a[1])
    elif a[0] == "publish":
        publish()
    elif a[0] == "score":
        score()
    elif a[0] == "live":
        live("--force" in a)
    elif a[0] == "lint":
        print(lint(json.loads(pathlib.Path(a[1]).read_text("utf-8")),
                   json.loads((OUT / f"{bid(a[2])}-data.json").read_text("utf-8"))) or "통과")
    else:
        fetch_save(a[0])
        build(a[0])
        if "--dry" not in a:
            publish()
            send_all(a[0])
