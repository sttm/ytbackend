import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote

# Tests never open the configured production database.
os.environ["PRODUCERSCENTER_BACKEND_DATABASE_URL"] = "sqlite:///:memory:"

from starlette.requests import Request

from app.api import streams
from app.schemas import YoutubeUrlRequest
from app.services import youtube


class MusicSearchTests(unittest.TestCase):
    def test_search_keeps_atv_ids_and_original_explicit_version(self):
        songs = [
            {"videoId": "explicit", "title": "Song", "videoType": "MUSIC_VIDEO_TYPE_ATV", "isExplicit": True},
            {"videoId": "clean", "title": "Song", "videoType": "ATV", "isExplicit": False},
            {"videoId": "video", "videoType": "MUSIC_VIDEO_TYPE_OMV", "isExplicit": True},
            {"videoId": "unknown", "isExplicit": True},
        ]
        client = MagicMock()
        client.search.side_effect = [songs, [], []]
        with patch.object(youtube, "YTMusic", return_value=client):
            items = youtube.search_media("Song", 10)
        self.assertEqual([i["id"] for i in items], ["explicit", "clean"])
        self.assertEqual([i["isExplicit"] for i in items], [True, False])
        self.assertEqual(items[0]["url"], "https://www.youtube.com/watch?v=explicit")

    def test_empty_or_failed_music_search_never_falls_back_to_videos(self):
        with patch.object(youtube, "search_youtube_music", return_value=[]), patch.object(youtube.yt_dlp, "YoutubeDL") as ydl:
            self.assertEqual(youtube.search_media("missing"), [])
            ydl.assert_not_called()
        with patch.object(youtube, "search_youtube_music", side_effect=RuntimeError("offline")), patch.object(youtube.yt_dlp, "YoutubeDL") as ydl:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                youtube.search_media("missing")
            ydl.assert_not_called()

    def test_album_failure_does_not_discard_songs(self):
        client = MagicMock()
        client.search.side_effect = [[{"videoId": "song", "videoType": "ATV"}], RuntimeError("album failure"), []]
        with patch.object(youtube, "YTMusic", return_value=client):
            self.assertEqual(youtube.search_media("Song")[0]["id"], "song")

    def test_old_cache_is_rejected_but_short_valid_results_are_reused(self):
        self.assertTrue(streams._is_stale_search_cache("youtube-music", [{"id": "old"}], 40))
        self.assertFalse(streams._is_stale_search_cache("youtube-music", [{"id": "song", "videoType": "ATV", "isExplicit": False}], 40))


class AudioFormatTests(unittest.TestCase):
    def test_prefers_m4a_and_never_proxies_a_manifest_as_audio(self):
        m4a = {"url": "https://cdn.test/audio", "format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2", "vcodec": "none", "protocol": "https"}
        opus = {**m4a, "format_id": "251", "ext": "webm", "acodec": "opus", "abr": 160}
        manifest = {**m4a, "protocol": "m3u8_native", "abr": 999}
        client = MagicMock()
        client.__enter__.return_value.extract_info.return_value = {"id": "song", "formats": [manifest, opus, m4a]}
        with patch.object(youtube.yt_dlp, "YoutubeDL", return_value=client):
            result = youtube.extract_best_audio("https://www.youtube.com/watch?v=song")
        self.assertEqual(result["format_id"], "140")
        self.assertFalse(youtube.is_audio_format(manifest))


class MediaResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_cdn_failure_retries_through_proxy_for_both_routes(self):
        for route in ("playback", "download"):
            with self.subTest(route=route):
                metadata = {"stream_url": "https://cdn.test/audio", "title": "Song", "ext": "m4a"}
                proxy_metadata = {**metadata, "stream_url": "https://cdn.test/retry", "proxy_used": "http://proxy.test:8080"}
                failed_session = MagicMock()
                failed_session.get = AsyncMock(side_effect=RuntimeError("HTTP 403"))
                failed_session.close = AsyncMock()
                response = MagicMock()
                response.status = 200
                response.headers = {}

                async def audio_chunks(_size):
                    yield b"audio"

                response.content.iter_chunked.side_effect = audio_chunks
                session = MagicMock()
                session.get = AsyncMock(return_value=response)
                session.close = AsyncMock()
                resolver = AsyncMock(side_effect=[metadata, proxy_metadata])
                request = Request({"type": "http", "headers": []})
                with patch.object(streams, "resolve_stream", resolver), patch.object(streams, "client_session_for_proxy", side_effect=[(failed_session, {}), (session, {})]) as sessions, patch.object(streams, "mark_proxy_media_success"), patch.object(streams, "mark_proxy_media_failure"):
                    if route == "playback":
                        result = await streams.playback(request, "https://youtu.be/song", client_ip=None, db=MagicMock())
                    else:
                        result = await streams.download(request, YoutubeUrlRequest(url="https://youtu.be/song"), MagicMock())
                    self.assertTrue(resolver.call_args.kwargs["prefer_proxy"])
                    self.assertTrue(resolver.call_args.kwargs["force_refresh"])
                    self.assertEqual(sessions.call_args.args[0], "http://proxy.test:8080")
                    self.assertEqual(b"".join([chunk async for chunk in result.body_iterator]), b"audio")
                failed_session.close.assert_awaited_once()
                session.close.assert_awaited_once()

    async def test_playback_and_download_stream_unicode_metadata_and_close_connections(self):
        for route in ("playback", "download"):
            with self.subTest(route=route):
                metadata = {"stream_url": "https://cdn.test/audio", "video_id": "song", "title": "Трек 100% 🎵", "artist": "Исполнитель", "ext": "m4a"}
                response = MagicMock()
                response.status = 206 if route == "playback" else 200
                response.headers = {"Content-Length": "5", "Content-Range": "bytes 0-4/5"}

                async def audio_chunks(_size):
                    yield b"audio"

                response.content.iter_chunked.side_effect = audio_chunks
                session = MagicMock()
                session.get = AsyncMock(return_value=response)
                session.close = AsyncMock()
                request = Request({"type": "http", "headers": [(b"range", b"bytes=0-4")]})
                with patch.object(streams, "resolve_stream", new=AsyncMock(return_value=metadata)), patch.object(streams, "client_session_for_proxy", return_value=(session, {})):
                    if route == "playback":
                        result = await streams.playback(request, "https://youtu.be/song", client_ip=None, db=MagicMock())
                        self.assertEqual(session.get.call_args.kwargs["headers"]["Range"], "bytes=0-4")
                    else:
                        result = await streams.download(request, YoutubeUrlRequest(url="https://youtu.be/song"), MagicMock())
                    self.assertEqual(unquote(result.headers["X-Track-Title"]), metadata["title"])
                    self.assertEqual(unquote(result.headers["X-Track-Artist"]), metadata["artist"])
                    self.assertEqual(result.status_code, response.status)
                    self.assertEqual(b"".join([chunk async for chunk in result.body_iterator]), b"audio")
                response.close.assert_called_once()
                session.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
