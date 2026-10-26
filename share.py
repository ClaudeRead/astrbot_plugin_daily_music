"""分享功能：识别私聊/群聊里发给 Bot 的歌曲/歌单链接，去听一听并按人格给评价；
也支持 Bot 主动分享歌曲/歌单链接。

缓存优先：评价复用 feeling_generator，其 system 提示词逐字不变，可变内容
（歌名、歌手、人格、标题）一律放进用户消息，只让尾部未命中。
"""

import random
import re
from dataclasses import dataclass

from .http_util import fetch_json
from .music_source import NETEASE_HEADERS, QQ_HEADERS, Track

# 分享一个歌单时，最多抽样听几首（控制单次 LLM 调用体量）
MAX_SHARE_TRACKS = 5
# 分享场景下模拟「听完整首」的时长（秒），写进听后感情境
SHARE_LISTEN_SECONDS = 210

NETEASE_SONG_URL = "https://music.163.com/api/song/detail"
NETEASE_PLAYLIST_URL = "https://music.163.com/api/playlist/detail"
QQ_SONG_URL = "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"
QQ_PLAYLIST_URL = "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"


@dataclass
class ShareLink:
    """从消息里识别出的音乐链接。"""

    platform: str   # netease / qq / qishui
    kind: str       # song / playlist
    sid: str        # 平台内部 ID（网易/QQ 有效；汽水无公开 ID，为空字符串）


# 链接正则，按「网易云 → QQ → 汽水」优先级依次匹配，命中即返回。
_NETEASE_SONG = re.compile(
    r"music\.163\.com/(?:[^\s/]*/)*song(?:\?id=|/)(\d+)", re.IGNORECASE
)
_NETEASE_PLAYLIST = re.compile(
    r"music\.163\.com/(?:[^\s/]*/)*playlist(?:\?id=|/)(\d+)", re.IGNORECASE
)
_QQ_SONG = re.compile(
    r"y\.qq\.com/(?:n/ryqq/songDetail/|n/yqq/song/)([0-9A-Za-z]+)", re.IGNORECASE
)
_QQ_SONG_MID = re.compile(r"songmid=([0-9A-Za-z]+)", re.IGNORECASE)
_QQ_PLAYLIST = re.compile(r"y\.qq\.com/n/ryqq/playlist/(\d+)", re.IGNORECASE)
# 汽水音乐（抖音系）无公开接口，识别域名即可，sid 留空
_QISHUI = re.compile(
    r"(?:soda\.douyin\.com|v\.douyin\.com|douyin\.com/music)/[^\s]+", re.IGNORECASE
)

_PATTERNS = (
    (_NETEASE_SONG, "netease", "song"),
    (_NETEASE_PLAYLIST, "netease", "playlist"),
    (_QQ_SONG, "qq", "song"),
    (_QQ_SONG_MID, "qq", "song"),
    (_QQ_PLAYLIST, "qq", "playlist"),
    (_QISHUI, "qishui", "song"),
)


def parse_share_link(text: str) -> ShareLink | None:
    """从消息文本里识别第一个音乐链接；没有命中返回 None。"""
    text = str(text or "")
    if not text:
        return None
    for pattern, platform, kind in _PATTERNS:
        m = pattern.search(text)
        if m:
            sid = m.group(1) if m.groups() else ""
            return ShareLink(platform=platform, kind=kind, sid=sid)
    return None


def playlist_url(platform: str, pid: str) -> str:
    """按平台生成歌单网页链接（网易云 / QQ音乐）。"""
    if platform == "netease":
        return f"https://music.163.com/playlist?id={pid}"
    if platform == "qq":
        return f"https://y.qq.com/n/ryqq/playlist/{pid}"
    return ""


async def resolve_tracks(link: ShareLink) -> list[Track]:
    """把识别出的链接解析成 Track 列表；汽水音乐无公开接口，抛 ValueError。"""
    if link.platform == "netease":
        return await _resolve_netease(link)
    if link.platform == "qq":
        return await _resolve_qq(link)
    raise ValueError("汽水音乐没有公开的歌曲信息接口")


async def _resolve_netease(link: ShareLink) -> list[Track]:
    if link.kind == "song":
        data = await fetch_json(
            NETEASE_SONG_URL,
            params={"ids": f"[{link.sid}]"},
            headers=NETEASE_HEADERS,
        )
        raw = (data.get("songs") or [])[:1]
    else:
        data = await fetch_json(
            NETEASE_PLAYLIST_URL,
            params={"id": link.sid},
            headers=NETEASE_HEADERS,
        )
        playlist = data.get("result") or data.get("playlist") or {}
        raw = playlist.get("tracks") or []
        if len(raw) > MAX_SHARE_TRACKS:
            raw = random.sample(raw, MAX_SHARE_TRACKS)
    return [
        Track(
            platform="netease",
            platform_name="网易云",
            name=str(t.get("name") or ""),
            artist=((t.get("artists") or [{}])[0].get("name") or ""),
            album=(t.get("album") or {}).get("name") or "",
            url=f"https://music.163.com/song?id={t.get('id')}",
            song_id=str(t.get("id") or ""),
        )
        for t in raw
        if t.get("name")
    ]


async def _resolve_qq(link: ShareLink) -> list[Track]:
    if link.kind == "song":
        data = await fetch_json(
            QQ_SONG_URL,
            params={"platform": "yqq", "format": "json", "songmid": link.sid},
            headers=QQ_HEADERS,
        )
        raw = (data.get("data") or [])[:1]
    else:
        data = await fetch_json(
            QQ_PLAYLIST_URL,
            params={
                "type": 1, "json": 1, "utf8": 1, "onlysong": 0,
                "disstid": link.sid, "format": "json",
            },
            headers=QQ_HEADERS,
        )
        raw = ((data.get("cdlist") or [{}])[0]).get("songlist") or []
        if len(raw) > MAX_SHARE_TRACKS:
            raw = random.sample(raw, MAX_SHARE_TRACKS)
    return [_qq_track(s) for s in raw if (s.get("name") or s.get("songname"))]


def _qq_track(s: dict) -> Track:
    """兼容 QQ 两个接口的字段差异：单曲详情的 singer/album 是对象，歌单里是数组/字符串。"""
    name = str(s.get("name") or s.get("songname") or "")
    singer = s.get("singer")
    if isinstance(singer, dict):
        artist = str(singer.get("name") or "")
    elif isinstance(singer, list) and singer:
        artist = str(singer[0].get("name") or "")
    else:
        artist = ""
    album = s.get("album")
    album_name = album.get("name") if isinstance(album, dict) else (s.get("albumname") or "")
    mid = str(s.get("mid") or s.get("songmid") or "")
    return Track(
        platform="qq",
        platform_name="QQ音乐",
        name=name,
        artist=artist,
        album=str(album_name or ""),
        url=f"https://y.qq.com/n/ryqq/songDetail/{mid}" if mid else "",
        song_id=mid,
    )
