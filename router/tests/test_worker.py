from __future__ import annotations

import os
import inspect
import re
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

TEST_ROOT = Path(tempfile.mkdtemp(prefix="relay-worker-tests-"))
os.environ.setdefault("DB_PATH", str(TEST_ROOT / "relay.db"))
os.environ.setdefault("SESSION_SECRET", "test-session-secret-that-is-long-enough")
os.environ.setdefault("BOOTSTRAP_TOKEN", "bootstrap-test-code")
os.environ.setdefault("FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
os.environ.setdefault("MEDIA_INTERNAL_USER", "internal")
os.environ.setdefault("MEDIA_INTERNAL_PASS", "internal-password")
os.environ.setdefault("PUBLIC_HOST", "relay.example.test")

from fastapi import HTTPException

from app import main


def joined(args: list[str]) -> str:
    return " ".join(args)


# A platform stored before a rename, or one added without touching
# build_audio_args. It has to be routed like the safe majority rather than
# crash or map nothing.
UNRECOGNISED = "something-new"

PLATFORMS = ("twitch", "youtube", "x", "rplay", "custom", UNRECOGNISED)

# The two destinations whose audio outlives the broadcast: YouTube runs Content
# ID over the archive and X auto-publishes the replay. They are the only two
# that take the clean track, and so the only two with a choice to make when the
# clean track is missing -- every other platform maps track 1 either way.
CLEAN_TRACK_PLATFORMS = ("youtube", "x")
TRACK_ONE_PLATFORMS = ("twitch", "rplay", "custom", UNRECOGNISED)

# Every destination is a straight forward, so every argument list ends the same
# way; only the -map set ahead of it varies.
COPY_TAIL = ["-c", "copy", "-muxdelay", "0", "-muxpreload", "0"]

# The four shapes an argument list can take.
TWITCH_DUAL_ARGS = ["-map", "0:v:0", "-map", "0:a:0", "-map", "0:a:1", *COPY_TAIL]
CLEAN_MIX_ARGS = ["-map", "0:v:0", "-map", "0:a:1", *COPY_TAIL]
FULL_MIX_ARGS = ["-map", "0:v:0", "-map", "0:a:0", *COPY_TAIL]
SILENT_ARGS = ["-map", "0:v:0", *COPY_TAIL]

# What `music_fallback` is when nobody passes it. It is the whole decision this
# table encodes, so it is named once here and pinned against the real signature
# in test_the_signature_is_platform_layout_and_an_override_that_defaults_on.
DEFAULT_MUSIC_FALLBACK = True

# The full contract, written out rather than derived: a diff here is a change to
# what viewers hear, and it should read like one.
#
# platform -> (two or more tracks, one track with the fallback OFF, one track
# with the fallback ON). The third column is the default one -- an omitted
# music_fallback lands there -- so it is the column that describes what a
# misconfigured OBS actually publishes. A video-only publisher is silent on
# every platform under either fallback, so it needs no column.
EXPECTED = {
    "twitch": (TWITCH_DUAL_ARGS, FULL_MIX_ARGS, FULL_MIX_ARGS),
    "youtube": (CLEAN_MIX_ARGS, SILENT_ARGS, FULL_MIX_ARGS),
    "x": (CLEAN_MIX_ARGS, SILENT_ARGS, FULL_MIX_ARGS),
    "rplay": (FULL_MIX_ARGS, FULL_MIX_ARGS, FULL_MIX_ARGS),
    "custom": (FULL_MIX_ARGS, FULL_MIX_ARGS, FULL_MIX_ARGS),
    UNRECOGNISED: (FULL_MIX_ARGS, FULL_MIX_ARGS, FULL_MIX_ARGS),
}

# The layouts a publisher can present: unknown, video-only, and one through the
# six audio tracks OBS can send.
LAYOUTS = (None, 0, 1, 2, 3, 6)
# None means "not known yet", which must be treated as the documented two-track
# setup rather than degraded.
MULTI_TRACK_LAYOUTS = (None, 2, 3, 6)
FALLBACKS = (False, True)


def expected_args(
    platform: str, tracks: int | None, music_fallback: bool = DEFAULT_MUSIC_FALLBACK
) -> list[str]:
    """Look the cell up in EXPECTED; the table above is the actual contract.

    The default here mirrors the default on build_audio_args on purpose, so
    calling both with the third argument omitted compares like with like.
    """
    multi, single, single_with_fallback = EXPECTED[platform]
    if tracks == 0:
        return SILENT_ARGS
    if tracks == 1:
        return single_with_fallback if music_fallback else single
    return multi


class AudioMappingTest(unittest.TestCase):
    """What each platform is handed is decided by the platform, nothing else.

    OBS owns every mix: track 1 is the full live mix (music + game + voice) and
    track 2 is the clean mix (game + voice). The relay only chooses which of
    them to forward, and forwards them untouched. The single exception is
    `music_fallback`, which does not choose a track -- it only says whether a
    YouTube or X destination may fall back to track 1 when the clean track is
    not there at all. It defaults to True, so that fallback is what happens
    unless somebody opts out.
    """

    def test_the_whole_platform_layout_and_fallback_matrix(self) -> None:
        """Every cell of EXPECTED, asserted as an exact argument list."""
        for platform in PLATFORMS:
            for tracks in LAYOUTS:
                for fallback in FALLBACKS:
                    self.assertEqual(
                        main.build_audio_args(platform, tracks, fallback),
                        expected_args(platform, tracks, fallback),
                        f"{platform}, {tracks} tracks, music_fallback={fallback}",
                    )

    def test_the_same_matrix_again_with_the_override_left_off_the_call(self) -> None:
        """The matrix above passes the override explicitly every time, so it
        cannot see the default at all -- and the default is the only thing this
        change moved. Walk the table again without passing it: every layout has
        to land on the ON column, which for the one-track row is the difference
        between publishing track 1 and publishing no audio track at all."""
        for platform in PLATFORMS:
            for tracks in LAYOUTS:
                self.assertEqual(
                    main.build_audio_args(platform, tracks),
                    expected_args(platform, tracks),
                    f"{platform}, {tracks} tracks, music_fallback omitted",
                )
                self.assertEqual(
                    main.build_audio_args(platform, tracks),
                    main.build_audio_args(platform, tracks, DEFAULT_MUSIC_FALLBACK),
                    f"{platform}, {tracks} tracks, music_fallback omitted",
                )

    def test_platform_alone_decides_which_obs_tracks_are_mapped(self) -> None:
        # Twitch takes both because Enhanced RTMP multitrack carries track 2 as
        # the separate VOD track. YouTube runs Content ID over the archive and X
        # auto-publishes the replay, so both get the clean track. RPLAY and
        # custom stay unpublished, so they get the full live experience.
        for platform in PLATFORMS:
            multi = EXPECTED[platform][0]
            for tracks in (2, 3, 6):
                for fallback in FALLBACKS:
                    self.assertEqual(
                        main.build_audio_args(platform, tracks, fallback),
                        multi,
                        f"{platform}, {tracks} tracks, music_fallback={fallback}",
                    )

    def test_unknown_layout_assumes_the_documented_two_track_setup(self) -> None:
        """A worker usually starts before OBS connects, so the layout is
        routinely unknown; degrading then would permanently strip Twitch's VOD
        track and silence YouTube and X for the whole broadcast."""
        for platform in PLATFORMS:
            for fallback in FALLBACKS:
                self.assertEqual(
                    main.build_audio_args(platform, None, fallback),
                    EXPECTED[platform][0],
                    f"{platform}, music_fallback={fallback}",
                )

    def test_extra_obs_tracks_ride_along_but_are_never_mapped(self) -> None:
        """OBS can publish six tracks and MediaMTX carries all of them. Mapping
        one nobody asked for would push an unwanted mix out to a platform."""
        for platform in PLATFORMS:
            for tracks in (3, 6):
                for fallback in FALLBACKS:
                    args = joined(main.build_audio_args(platform, tracks, fallback))
                    for index in range(2, 6):
                        self.assertNotIn(
                            f"0:a:{index}",
                            args,
                            f"{platform}, {tracks} tracks, music_fallback={fallback}",
                        )

    # -- The one-track degrade ----------------------------------------------
    #
    # These four tests are the reason the third argument exists. A publisher
    # sending one track is a broken OBS setup, and the relay has to choose which
    # way to be wrong about it: carry track 1 -- the music mix -- to a scanned
    # archive, or emit FLV with no audio track at all. The default is track 1,
    # because a no-audio-track FLV has never been verified against the YouTube
    # or X ingests, and a stream the service rejects outright, or archives
    # silent, is the worse and far likelier failure. The cost is real and known:
    # Content ID reads the YouTube archive and X auto-publishes the replay. The
    # compensating control is that the dashboard has to show this state loudly,
    # which lives in site/app/page.tsx, not here.

    def test_one_track_obs_carries_the_music_mix_to_youtube_or_x_by_default(self) -> None:
        """Track 1 carries the music, and this is where it goes when the clean
        track is missing and nobody opted out. Keeping a track on the wire beats
        an FLV stream with no audio track at all, whose acceptance by either
        ingest is unverified -- and the broadcaster has to fix the one-track
        setup either way."""
        for platform in CLEAN_TRACK_PLATFORMS:
            args = main.build_audio_args(platform, 1, True)
            self.assertEqual(args, FULL_MIX_ARGS, platform)
            # Stated twice on purpose: the music mix, specifically, and not the
            # clean track the platform normally takes.
            self.assertIn("0:a:0", joined(args), platform)
            self.assertNotIn("0:a:1", joined(args), platform)

    def test_one_track_obs_goes_silent_on_youtube_or_x_only_when_opted_out(self) -> None:
        """False is the explicit opt-out, for an operator who would rather have
        a silent archive than one carrying the music mix. It is the only way a
        YouTube or X destination drops the audio map entirely, and nobody
        reaches it by accident."""
        for platform in CLEAN_TRACK_PLATFORMS:
            for args in (
                main.build_audio_args(platform, 1, False),
                main.build_audio_args(platform, 1, music_fallback=False),
            ):
                self.assertEqual(args, SILENT_ARGS, platform)
                # Stated twice on purpose: no audio map of any kind, not merely
                # a different one.
                self.assertNotIn("0:a:", joined(args), platform)

    def test_the_music_fallback_defaults_to_on_when_the_caller_omits_it(self) -> None:
        """The default is the decision. A call site that omits the third
        argument must get track 1 on YouTube and X -- silence is only ever
        reached by asking for it, never by forgetting an argument."""
        for platform in CLEAN_TRACK_PLATFORMS:
            for args in (
                main.build_audio_args(platform, 1),
                main.build_audio_args(platform, audio_tracks=1),
            ):
                self.assertEqual(args, FULL_MIX_ARGS, platform)
                self.assertIn("0:a:0", joined(args), platform)

    def test_the_music_fallback_cannot_change_a_stream_that_has_both_tracks(self) -> None:
        """The override answers one question only -- what to do when the clean
        track is missing. Turning it on must never reroute a healthy two-track
        stream from the clean mix onto the music mix."""
        for platform in PLATFORMS:
            for tracks in MULTI_TRACK_LAYOUTS:
                self.assertEqual(
                    main.build_audio_args(platform, tracks, True),
                    main.build_audio_args(platform, tracks, False),
                    f"{platform} with {tracks} tracks",
                )

    # -----------------------------------------------------------------------

    def test_a_single_track_publisher_degrades_to_track_one_where_track_one_is_normal(self) -> None:
        """Referencing 0:a:1 against a publisher that is not sending it fails
        the whole command, so Twitch gives up its VOD track rather than the
        broadcast. Twitch, RPLAY and custom take track 1 in the normal case too,
        so degrading to it inverts nothing and the fallback is irrelevant."""
        for platform in TRACK_ONE_PLATFORMS:
            for fallback in FALLBACKS:
                self.assertEqual(
                    main.build_audio_args(platform, 1, fallback),
                    FULL_MIX_ARGS,
                    f"{platform}, music_fallback={fallback}",
                )

    def test_a_video_only_publisher_gets_no_audio_map(self) -> None:
        """audio_track_count sums to 0 for a video-only path rather than
        returning None, so mapping 0:a:0 unconditionally failed the command on
        every destination of that stream and parked them all in retrying. The
        fallback cannot conjure a track out of a publisher that has none."""
        for platform in PLATFORMS:
            for fallback in FALLBACKS:
                self.assertEqual(
                    main.build_audio_args(platform, 0, fallback),
                    SILENT_ARGS,
                    f"{platform}, music_fallback={fallback}",
                )

    def test_nothing_is_ever_mixed_or_re_encoded(self) -> None:
        """The whole point of the change: OBS makes every mix and the relay
        forwards it. Any filter graph here re-encodes audio the streamer already
        finished, and skews it -- amix pairs inputs by sample index, so two RTSP
        audio streams that start at different offsets stay skewed for life."""
        forbidden = ("-filter_complex", "-c:a", "aac", "amix", "aresample", "alimiter")
        for platform in PLATFORMS:
            for tracks in LAYOUTS:
                # The omitted call is walked too: it is the shape the default
                # decides, and nothing about that decision may reintroduce a
                # filter graph as a way of covering a missing track.
                calls = [main.build_audio_args(platform, tracks)]
                calls += [main.build_audio_args(platform, tracks, f) for f in FALLBACKS]
                for args in calls:
                    # Substring, not element membership: a filter graph arrives
                    # as a single argv element, so a list check would miss it
                    # entirely.
                    text = joined(args)
                    for token in forbidden:
                        self.assertNotIn(token, text, f"{platform}, {tracks} tracks: {text}")

    def test_every_output_is_a_video_map_followed_by_a_stream_copy(self) -> None:
        for platform in PLATFORMS:
            for tracks in LAYOUTS:
                for fallback in FALLBACKS:
                    args = main.build_audio_args(platform, tracks, fallback)
                    label = f"{platform}, {tracks} tracks, music_fallback={fallback}"
                    self.assertEqual(args[:2], ["-map", "0:v:0"], label)
                    self.assertEqual(args[-6:], COPY_TAIL, label)

    def test_an_unrecognised_platform_forwards_the_full_live_mix(self) -> None:
        """A destination stored before a platform was renamed, or one added
        without touching this function, must still forward something sane -- and
        must not be treated as a clean-track platform, which would silence it."""
        self.assertEqual(main.build_audio_args(UNRECOGNISED, 2, False), FULL_MIX_ARGS)
        self.assertEqual(main.build_audio_args(UNRECOGNISED, 1, False), FULL_MIX_ARGS)
        # Including on the default path, where the clean-track platforms behave
        # differently and an unrecognised name must not drift into that set.
        self.assertEqual(main.build_audio_args(UNRECOGNISED, 1), FULL_MIX_ARGS)

    def test_the_worker_passes_the_platform_the_layout_and_the_stored_override(self) -> None:
        """The three arguments come from three different places -- the platform
        column, the live path, and the destination's own stored flag. They are
        all positional, so a swapped pair would still run and would silently
        reroute audio."""
        flattened = " ".join(inspect.getsource(main.WorkerManager._run).split())
        call = re.search(r"build_audio_args\(((?:[^()]|\([^()]*\))*)\)", flattened)
        self.assertIsNotNone(call, "the worker no longer calls build_audio_args")
        arguments = call.group(1)
        positions = []
        for text in ('row["platform"]', "audio_track_count(media)", 'bool(row["music_fallback"])'):
            position = arguments.find(text)
            self.assertNotEqual(position, -1, f"{text} is not passed to build_audio_args")
            positions.append(position)
        self.assertEqual(positions, sorted(positions), f"wrong argument order: {arguments}")

    def test_the_signature_is_platform_layout_and_an_override_that_defaults_on(self) -> None:
        signature = inspect.signature(main.build_audio_args)
        self.assertEqual(
            list(signature.parameters),
            ["platform", "audio_tracks", "music_fallback"],
        )
        # The default is itself the decision, not an implementation detail:
        # flipping it here changes what every caller that omits the argument
        # publishes, without changing a single branch. Pinned as an identity
        # check so a truthy stand-in cannot pass for it.
        self.assertIs(signature.parameters["music_fallback"].default, True)
        # ...and the EXPECTED table's default column has to agree with it.
        self.assertIs(signature.parameters["music_fallback"].default, DEFAULT_MUSIC_FALLBACK)

    def test_the_per_destination_audio_mode_api_is_gone(self) -> None:
        """Five user-selectable modes existed and were deleted; a leftover
        reference would mean part of the old routing is still reachable. The
        music fallback is not a sixth: it picks no track, it only says whether
        a missing clean track may degrade to track 1."""
        for name in (
            "AUDIO_PASSTHROUGH_DUAL",
            "AUDIO_MODES_MIXED",
            "AUDIO_MODES_TWITCH_ONLY",
            "AUDIO_MODE_MAX",
            "audio_mode_allowed",
            "AudioTrackBody",
            "change_destination_audio",
        ):
            self.assertFalse(hasattr(main, name), name)


class MusicFallbackValidationTest(unittest.TestCase):
    """Only YouTube and X have a fallback to choose; the rest map track 1 anyway.

    Storing True on a platform that ignores it would read back through
    /api/state as an ON control that changes nothing, and on this setting a
    wrong belief about what is being published is the whole failure mode. So it
    is rejected rather than silently ignored.
    """

    def test_the_override_is_accepted_where_it_means_something(self) -> None:
        for platform in CLEAN_TRACK_PLATFORMS:
            main.validate_music_fallback(True, platform)
            main.validate_music_fallback(False, platform)

    def test_turning_it_on_elsewhere_is_refused_not_ignored(self) -> None:
        for platform in ("twitch", "rplay", "custom", UNRECOGNISED):
            with self.assertRaises(HTTPException, msg=platform) as raised:
                main.validate_music_fallback(True, platform)
            self.assertEqual(raised.exception.status_code, 422, platform)

    def test_off_is_harmless_everywhere_and_stays_accepted(self) -> None:
        """False is no longer the schema default -- it is the opt-out -- but it
        still changes nothing outside YouTube and X, so every platform has to
        tolerate being handed it. A create form that sends the field
        unconditionally depends on this."""
        for platform in PLATFORMS:
            main.validate_music_fallback(False, platform)


class MusicFallbackResolutionTest(unittest.TestCase):
    """The trap the flipped default introduced, and the guard against it.

    The stored default is now on, and the obvious way to spell that on the
    request model -- `music_fallback: bool = True` -- would hand True to
    validate_music_fallback on every Twitch, RPLAY and custom create and 422 a
    field those forms never send. The model field is tri-state instead, and
    resolve_music_fallback() is the only thing that turns "nothing was chosen"
    into a stored value. Folding the validation into the resolver is the point:
    a future call site cannot forget the explicit-only guard by forgetting a
    separate call.
    """

    def test_the_request_model_keeps_omitted_and_chosen_apart(self) -> None:
        omitted = main.DestinationBody(
            name="Twitch", platform="twitch", output_url="twitch-stream-key-123"
        )
        # None, not False and not True: "this form never showed the control" has
        # to stay distinguishable from either real answer, or the resolver
        # cannot tell them apart either.
        self.assertIsNone(omitted.music_fallback)
        chosen = main.DestinationBody(
            name="YouTube",
            platform="youtube",
            output_url="youtube-stream-key-123",
            music_fallback=False,
        )
        self.assertIs(chosen.music_fallback, False)

    def test_an_omitted_flag_never_rejects_a_platform_that_ignores_it(self) -> None:
        """The specific regression the flip introduces: a create that never
        mentioned music_fallback must not 422 merely because the stored default
        is now on."""
        for platform in PLATFORMS:
            main.resolve_music_fallback(None, platform)

    def test_an_omitted_flag_resolves_to_the_platform_default(self) -> None:
        for platform in CLEAN_TRACK_PLATFORMS:
            self.assertIs(main.resolve_music_fallback(None, platform), True, platform)
        for platform in TRACK_ONE_PLATFORMS:
            # Off, not on: a stored 1 has to keep meaning "a live YouTube or X
            # opt-in", never a value left on a row that cannot act on it.
            self.assertIs(main.resolve_music_fallback(None, platform), False, platform)

    def test_an_explicit_choice_is_still_validated_not_waved_through(self) -> None:
        """The flip moved the default, not the rule. Storing True on a platform
        that ignores it would still read back through /api/state as an ON
        control that changes nothing."""
        for platform in CLEAN_TRACK_PLATFORMS:
            self.assertIs(main.resolve_music_fallback(True, platform), True, platform)
            self.assertIs(main.resolve_music_fallback(False, platform), False, platform)
        for platform in TRACK_ONE_PLATFORMS:
            with self.assertRaises(HTTPException, msg=platform) as raised:
                main.resolve_music_fallback(True, platform)
            self.assertEqual(raised.exception.status_code, 422, platform)
            self.assertIs(main.resolve_music_fallback(False, platform), False, platform)

    def test_the_create_route_resolves_before_it_stores(self) -> None:
        """A route that called validate_music_fallback directly would be back in
        the trap, and one that skipped both would store an unsettable 1."""
        source = inspect.getsource(main.add_destination)
        self.assertIn("resolve_music_fallback(body.music_fallback, body.platform)", source)


class AudioTrackCountTest(unittest.TestCase):
    def test_video_tracks_are_not_counted_as_audio(self) -> None:
        media = {
            "known": True,
            "available": True,
            "tracks": ["H264", "MPEG-4 Audio", "MPEG-4 Audio"],
        }
        self.assertEqual(main.audio_track_count(media), 2)

    def test_tracks2_is_preferred_when_present(self) -> None:
        media = {
            "known": True,
            "available": True,
            "tracks": ["H264", "MPEG-4 Audio"],
            "tracks2": [
                {"codec": "H264", "codecProps": {"width": 1920, "height": 1080}},
                {"codec": "MPEG4Audio", "codecProps": {"sampleRate": 48000, "channelCount": 2}},
            ],
        }
        self.assertEqual(main.audio_track_count(media), 1)

    def test_a_video_only_publisher_counts_zero_not_none(self) -> None:
        """This is the input side of the video-only bug: 0 means "there is no
        audio to map", where None means "not known yet, assume two tracks"."""
        self.assertEqual(
            main.audio_track_count({"known": True, "available": True, "tracks": ["H264"]}), 0
        )
        self.assertEqual(
            main.audio_track_count(
                {
                    "known": True,
                    "available": True,
                    "tracks": ["H264"],
                    "tracks2": [{"codec": "H264", "codecProps": {"width": 1920, "height": 1080}}],
                }
            ),
            0,
        )

    def test_unknown_or_absent_path_reports_none(self) -> None:
        self.assertIsNone(main.audio_track_count({"known": False, "available": False}))
        self.assertIsNone(main.audio_track_count({"known": True, "available": False}))
        self.assertIsNone(main.audio_track_count({"known": True, "available": True, "tracks": []}))


class DestinationUrlTest(unittest.TestCase):
    def test_youtube_backup_marker_belongs_to_the_app_not_the_key(self) -> None:
        row = {
            "platform": "youtube",
            "output_url_enc": main.encrypt("abcd-efgh-ijkl-mnop"),
        }
        primary, backup = main.workers._destination_urls(row)
        self.assertEqual(primary, f"{main.YOUTUBE_PRIMARY_BASE_URL}/abcd-efgh-ijkl-mnop")
        # FFmpeg parses everything after the last path element as the playpath,
        # so a trailing ?backup=1 would be sent as part of the stream name.
        self.assertEqual(backup, f"{main.YOUTUBE_BACKUP_BASE_URL}?backup=1/abcd-efgh-ijkl-mnop")

    def test_a_full_address_is_used_verbatim(self) -> None:
        row = {"platform": "twitch", "output_url_enc": main.encrypt("rtmps://example.test/app/key")}
        primary, backup = main.workers._destination_urls(row)
        self.assertEqual(primary, "rtmps://example.test/app/key")
        self.assertIsNone(backup)


class RedactionTest(unittest.TestCase):
    def test_destination_urls_are_replaced(self) -> None:
        line = "rtmps://live.example/app/secret-key: Broken pipe"
        self.assertNotIn("secret-key", main.redact(line, "rtmps://live.example/app/secret-key"))

    def test_embedded_credentials_are_stripped(self) -> None:
        """The RTSP source URL carries the shared media password."""
        line = "rtsp://internal:internal-password@mediamtx:8554/studio: timeout"
        cleaned = main.redact(line)
        self.assertNotIn("internal-password", cleaned)
        self.assertIn("[redacted]", cleaned)

    def test_redaction_survives_a_missing_url(self) -> None:
        self.assertEqual(main.redact("plain text", None), "plain text")



class SlateVariantTest(unittest.TestCase):
    """Which failover slate a stream is handed.

    A slate that does not match the feed's geometry changes resolution or cadence
    mid-stream underneath `-c:v copy` every time OBS reconnects. Production ran
    for days with a single 720p48 slate standing in for two 1080p feeds, so these
    assert the mapping rather than trusting it.
    """

    def profile(self, **over):
        return {**main.DEFAULT_CONTRIBUTION, **over}

    def test_every_supported_geometry_maps_to_itself(self) -> None:
        for width, height, fps in main.SLATE_VARIANTS:
            self.assertEqual(
                main.snap_slate_variant(self.profile(width=width, height=height, fps=fps)),
                (width, height, fps),
                f"{width}x{height}p{fps:g}",
            )

    def test_the_two_production_feeds_get_their_own_geometry(self) -> None:
        # studio probes 1080p48; the second tenant probes 1080p60. Both were
        # being served the same 720p48 file.
        self.assertEqual(
            main.snap_slate_variant(self.profile(width=1920, height=1080, fps=48.0)),
            (1920, 1080, 48.0),
        )
        self.assertEqual(
            main.snap_slate_variant(self.profile(width=1920, height=1080, fps=60.0)),
            (1920, 1080, 60.0),
        )

    def test_height_decides_before_frame_rate(self) -> None:
        """A resolution change at the splice forces a decoder reconfiguration;
        a cadence change only makes the segmenter re-anchor. So a 1080p25 feed
        must land on 1080, not on the 720 variant with the nearer frame rate."""
        self.assertEqual(
            main.snap_slate_variant(self.profile(height=1080, fps=25.0)), (1920, 1080, 30.0)
        )
        self.assertEqual(
            main.snap_slate_variant(self.profile(height=720, fps=59.94)), (1280, 720, 60.0)
        )

    def test_unsupported_shapes_snap_to_the_nearest(self) -> None:
        for height, fps, expected in (
            (1440, 60.0, (1920, 1080, 60.0)),   # closer to 1080 than to 720
            (900, 30.0, (1920, 1080, 30.0)),    # exact tie: 180 lines either way, resolves up
            (480, 30.0, (1280, 720, 30.0)),
            (2160, 48.0, (1920, 1080, 48.0)),
            (1080, 24.0, (1920, 1080, 30.0)),
            (1080, 50.0, (1920, 1080, 48.0)),
            (1080, 55.0, (1920, 1080, 60.0)),
        ):
            self.assertEqual(
                main.snap_slate_variant(self.profile(height=height, fps=fps)), expected,
                f"{height}p{fps:g}",
            )

    def test_exact_ties_resolve_upward_by_rule_not_by_list_order(self) -> None:
        """900p is 180 lines from both 720 and 1080, and 54 fps is 6 from both 48
        and 60. Without an explicit rule the answer is whichever entry happens to
        come first in SLATE_VARIANTS, which is not a decision anyone made."""
        self.assertEqual(
            main.snap_slate_variant(self.profile(height=900, fps=30.0)), (1920, 1080, 30.0)
        )
        self.assertEqual(
            main.snap_slate_variant(self.profile(height=1080, fps=54.0)), (1920, 1080, 60.0)
        )

    def test_missing_or_junk_values_fall_back_to_the_defaults(self) -> None:
        """The profile is read back out of app_settings, so it can be absent or
        malformed; snapping must still produce a usable geometry."""
        default = main.snap_slate_variant(dict(main.DEFAULT_CONTRIBUTION))
        for bad in ({"height": None}, {"fps": None}, {"height": "wide"}, {"fps": "fast"}, {}):
            self.assertEqual(main.snap_slate_variant({**main.DEFAULT_CONTRIBUTION, **bad}), default)
        self.assertEqual(main.snap_slate_variant({}), default)

    def test_each_geometry_gets_its_own_cached_file(self) -> None:
        paths = {main.slate_variant_path(v) for v in main.SLATE_VARIANTS}
        self.assertEqual(len(paths), len(main.SLATE_VARIANTS), "variants must not share a file")
        self.assertEqual(
            main.slate_variant_path((1920, 1080, 48.0)).name,
            f"slate-1920x1080p48v{main.SLATE_ENCODER_VERSION}.mp4",
        )

    def test_a_recipe_bump_renames_the_cached_variants(self) -> None:
        """ensure_slate_variants only renders missing files, so the encoder
        version has to live in the filename or a recipe change keeps serving
        files rendered by the old recipe forever."""
        variant = (1920, 1080, 48.0)
        current = main.slate_variant_path(variant)
        with mock.patch.object(main, "SLATE_ENCODER_VERSION", main.SLATE_ENCODER_VERSION + 1):
            bumped = main.slate_variant_path(variant)
        self.assertNotEqual(current, bumped)

    def test_the_slate_recipe_never_emits_b_frames(self) -> None:
        """The file in production carried has_b_frames=2. B-frames make PTS != DTS,
        which the FLV muxer rejects, so no slate may have them whatever the feed does."""
        args = main.slate_encode_args({**main.DEFAULT_CONTRIBUTION, "bframes": 3})
        self.assertIn("-bf", args)
        self.assertEqual(args[args.index("-bf") + 1], "0")


class ProgressParsingTest(unittest.TestCase):
    # Everything one -progress block is normalised into, written out in full
    # because the absence is the point: `bitrate_kbps` is not here. ffmpeg's
    # `bitrate=` is total_size * 8 / out_time -- an average over the whole life
    # of the process -- and a panel labelled with what is happening now must
    # never be able to reach it by accident. The rate is derived from
    # total_bytes deltas by derive_output_rate instead.
    SAMPLE_KEYS = {
        "total_bytes", "frames", "fps", "speed",
        "drop_frames", "dup_frames", "out_time_s",
    }

    def test_units_are_stripped(self) -> None:
        sample = main.parse_progress_block(
            {
                "frame": "1200",
                "fps": "48.0",
                "bitrate": "6021.4kbits/s",
                "total_size": "4500000",
                "out_time_us": "25000000",
                "speed": "1.00x",
                "drop_frames": "0",
                "dup_frames": "0",
            }
        )
        # The byte counter is the input to the rate derivation, so it is the
        # one field the panel now depends on being carried faithfully.
        self.assertEqual(sample["total_bytes"], 4_500_000)
        self.assertAlmostEqual(sample["speed"], 1.0)
        self.assertAlmostEqual(sample["out_time_s"], 25.0)
        self.assertEqual(sample["frames"], 1200)
        self.assertAlmostEqual(sample["fps"], 48.0)
        self.assertEqual(sample["drop_frames"], 0)
        self.assertEqual(sample["dup_frames"], 0)

    def test_the_lifetime_bitrate_field_is_not_carried_at_all(self) -> None:
        """A real block always contains `bitrate=`; nothing may pick it up.

        Dropping it here rather than merely ignoring it downstream is
        deliberate: while the key existed on the sample, every future caller
        looking for "the bitrate" found a number that meant "averaged since
        this process started" and was labelled as if it meant "now". A worker
        that spent its first minute on the failover slate reads hundreds of
        kbps low for the rest of the broadcast.
        """
        sample = main.parse_progress_block(
            {"bitrate": "6021.4kbits/s", "total_size": "4500000", "out_time_us": "25000000"}
        )
        self.assertNotIn("bitrate_kbps", sample)
        # Not smuggled in under some other name either.
        self.assertNotIn(6021.4, sample.values())
        self.assertEqual(set(sample), self.SAMPLE_KEYS)
        # And the parser itself no longer reads the field, so re-adding it is a
        # visible change here rather than a silent one.
        self.assertNotIn('number("bitrate")', inspect.getsource(main.parse_progress_block))

    def test_not_available_values_become_none(self) -> None:
        """`N/A` is what the tee muxer reports for total_size, and None is what
        makes the panel show nothing rather than a number it made up."""
        sample = main.parse_progress_block(
            {"total_size": "N/A", "speed": " N/A ", "frame": "N/A", "out_time_us": "N/A"}
        )
        self.assertIsNone(sample["total_bytes"])
        self.assertIsNone(sample["speed"])
        self.assertIsNone(sample["frames"])
        self.assertIsNone(sample["out_time_s"])

    def test_missing_keys_do_not_raise(self) -> None:
        sample = main.parse_progress_block({})
        self.assertIsNone(sample["total_bytes"])
        self.assertEqual(set(sample), self.SAMPLE_KEYS)
        self.assertEqual([value for value in sample.values() if value is not None], [])


# 750,000 bytes a second is 6,000,000 bits a second: a 6000 kbps destination,
# which is roughly what the Twitch worker in the bug report was actually
# pushing while the panel showed it climbing through 5350 and then 5967. Every
# expected number below is worked out by hand from this pair rather than by
# running the formula these tests exist to check.
STEADY_BYTES_PER_SECOND = 750_000
STEADY_KBPS = 6000.0

# The failover slate is tiny by comparison: 12,500 B/s is 100 kbps. Fifty-five
# seconds of it in front of a real feed is the exact shape that made the
# lifetime average unusable.
SLATE_BYTES_PER_SECOND = 12_500
SLATE_KBPS = 100.0

# Cumulative bytes for a feed whose keyframes land every other second: one
# second carries a whole 2 s GOP's worth of bytes and the next carries almost
# none. Sampled at -stats_period 1 and differenced against its neighbour this
# reads 12000 kbps, then 0, then 12000 -- the sawtooth that costs the panel its
# credibility. The mean is exactly STEADY_BYTES_PER_SECOND.
SAWTOOTH_TOTAL_BYTES = [
    0, 1_500_000, 1_500_000, 3_000_000, 3_000_000,
    4_500_000, 4_500_000, 6_000_000, 6_000_000,
]


class DerivedOutputRateTest(unittest.TestCase):
    """The rate the panel shows, derived from (monotonic, total_bytes) pairs.

    Pure and importable on purpose: the thing being asserted is arithmetic over
    a byte counter, and it should be provable without a subprocess, a socket or
    an event loop.
    """

    def test_a_steady_stream_reports_the_rate_it_is_actually_pushing(self) -> None:
        # 750,000 B/s over the 4 s window is 3,000,000 bytes; 3,000,000 * 8 /
        # 4 / 1000 = 6000 kbps.
        history = [(float(t), float(t * STEADY_BYTES_PER_SECOND)) for t in range(9)]
        self.assertAlmostEqual(main.derive_output_rate(history), STEADY_KBPS)

    def test_neither_clock_nor_counter_is_assumed_to_start_at_zero(self) -> None:
        """time.monotonic() has an arbitrary epoch and a worker resumed after a
        reconnect carries whatever byte count it had; only the deltas mean
        anything."""
        history = [
            (10_000.0 + t, 4_000_000.0 + t * STEADY_BYTES_PER_SECOND) for t in range(9)
        ]
        self.assertAlmostEqual(main.derive_output_rate(history), STEADY_KBPS)

    def test_nothing_to_difference_yet_is_reported_as_nothing(self) -> None:
        """One reading is a position, not a rate. None is the honest answer and
        the dashboard already renders it as a blank."""
        for history in ([], [(1.0, 0.0)], [(1.0, 5_000_000.0)]):
            with self.subTest(history=history):
                self.assertIsNone(main.derive_output_rate(history))

    def test_a_span_shorter_than_the_minimum_is_not_answered(self) -> None:
        """A one-second delta against a two-second GOP is the aliasing this
        change exists to remove, so it is refused rather than published."""
        for span in (0.5, 1.0, 1.9):
            history = [(0.0, 0.0), (span, span * STEADY_BYTES_PER_SECOND)]
            with self.subTest(span=span):
                self.assertIsNone(main.derive_output_rate(history))
        # Two seconds -- one whole keyframe interval -- is the shortest span
        # that gets an answer, so a fresh worker fills the panel in quickly
        # instead of sitting blank for the whole window.
        warm_up = [(0.0, 0.0), (2.0, 2.0 * STEADY_BYTES_PER_SECOND)]
        self.assertAlmostEqual(main.derive_output_rate(warm_up), STEADY_KBPS)

    def test_a_counter_that_went_backwards_reports_nothing_not_a_negative_rate(self) -> None:
        """ffmpeg's counter only ever climbs within one process, so a decrease
        means the history spans two of them. -2000 kbps on a dashboard is worse
        than a blank."""
        restarted = [(0.0, 5_000_000.0), (2.0, 5_400_000.0), (4.0, 100_000.0)]
        self.assertIsNone(main.derive_output_rate(restarted))
        falling = [(0.0, 5_000_000.0), (2.0, 4_000_000.0), (4.0, 3_000_000.0)]
        self.assertIsNone(main.derive_output_rate(falling))

    def test_a_clock_that_did_not_advance_returns_none_without_dividing_by_zero(self) -> None:
        """The span is a denominator. A ZeroDivisionError here would escape into
        the stdout reader task and stop the worker reporting anything at all --
        so it raising would fail this test just as loudly as a wrong number."""
        frozen = [(5.0, 0.0), (5.0, 750_000.0), (5.0, 1_500_000.0)]
        self.assertIsNone(main.derive_output_rate(frozen))
        backwards = [(10.0, 0.0), (4.0, 750_000.0)]
        self.assertIsNone(main.derive_output_rate(backwards))

    def test_a_sawtooth_feed_reads_as_its_mean_not_as_its_teeth(self) -> None:
        """The whole reason the window is seconds and not one -progress block.

        The fixture is a real feed shape: bytes leave in keyframe-sized lumps,
        so second-to-second the counter jumps and then stalls. A rate derived
        from adjacent samples swings between twice the truth and zero; a rate
        derived across the window lands on the truth.
        """
        history = [(float(t), float(total)) for t, total in enumerate(SAWTOOTH_TOTAL_BYTES)]

        # First prove the fixture really does have teeth, or the rest of this
        # test proves nothing: adjacent samples alternate 12000 kbps and 0.
        adjacent = [
            main.derive_output_rate(history[index - 1:index + 1], 1.0)
            for index in range(1, len(history))
        ]
        self.assertEqual(sorted(set(adjacent)), [0.0, 12000.0])

        # Once the window is full it spans two whole keyframe intervals, so it
        # lands exactly on the mean at every point in the run.
        for length in range(5, len(history) + 1):
            with self.subTest(samples=length):
                self.assertAlmostEqual(
                    main.derive_output_rate(history[:length]), STEADY_KBPS
                )

        # And while it is still filling, a partial window is still never worse
        # than the adjacent-sample view it replaces. (The 3 s prefix is the one
        # that does not divide evenly into 2 s keyframe intervals and so is not
        # exactly the mean -- it is the known price of answering early, and it
        # is still three times closer than the naive view is at that moment.)
        for length in range(3, len(history) + 1):
            windowed = main.derive_output_rate(history[:length])
            adjacent_view = main.derive_output_rate(history[length - 2:length], 1.0)
            with self.subTest(samples=length):
                self.assertLessEqual(
                    abs(windowed - STEADY_KBPS),
                    abs(adjacent_view - STEADY_KBPS),
                )

    def test_a_worker_that_began_on_the_slate_is_not_still_paying_for_it(self) -> None:
        """The reported bug, as arithmetic.

        Fifty-five seconds of the 96 KB failover slate followed by five seconds
        of the real feed. ffmpeg's own bitrate= would read 4,437,500 * 8 / 60 /
        1000 = 591.7 kbps and would keep creeping upward for minutes. The
        question the panel asks -- what is going out right now -- has one
        answer, and it is 6000.
        """
        history = []
        for second in range(61):
            slate_seconds = min(second, 55)
            live_seconds = max(second - 55, 0)
            history.append((
                float(second),
                float(
                    slate_seconds * SLATE_BYTES_PER_SECOND
                    + live_seconds * STEADY_BYTES_PER_SECOND
                ),
            ))
        lifetime_kbps = history[-1][1] * 8 / history[-1][0] / 1000
        self.assertAlmostEqual(lifetime_kbps, 591.666, places=2)

        rate = main.derive_output_rate(history)
        self.assertAlmostEqual(rate, STEADY_KBPS)
        # Stated as a relationship as well as a value: the failure being fixed
        # is not "off by a bit", it is a tenfold understatement that the panel
        # presented as the current rate.
        self.assertGreater(rate, lifetime_kbps * 5)

        # The same history read while the slate was still the only thing going
        # out reports the slate's own rate, which was the truth at the time.
        self.assertAlmostEqual(main.derive_output_rate(history[:40]), SLATE_KBPS)

    def test_the_history_it_is_handed_is_left_alone(self) -> None:
        """It is handed the manager's live deque on every -progress block."""
        history = deque(
            (float(t), float(t * STEADY_BYTES_PER_SECOND)) for t in range(9)
        )
        before = list(history)
        # A deque, specifically: that is what _read_progress passes, and it is
        # indexed from both ends by the derivation.
        self.assertAlmostEqual(main.derive_output_rate(history), STEADY_KBPS)
        self.assertEqual(list(history), before)

    def test_the_window_spans_keyframes_rather_than_single_samples(self) -> None:
        """The constants are the guard against the sawtooth coming back. A GOP
        is 2 s on both Twitch and YouTube, so the window has to cover two of
        them to average whole keyframe intervals, and the warm-up floor has to
        cover at least one."""
        gop_seconds = main.DEFAULT_CONTRIBUTION["gop_seconds"]
        self.assertGreaterEqual(main.METRICS_OUTPUT_RATE_WINDOW_S, gop_seconds * 2)
        self.assertGreaterEqual(main.METRICS_OUTPUT_RATE_MIN_SPAN_S, gop_seconds)
        self.assertLessEqual(
            main.METRICS_OUTPUT_RATE_MIN_SPAN_S, main.METRICS_OUTPUT_RATE_WINDOW_S
        )
        # And the callable's own default is that constant, so the pure tests
        # above and the running worker are measuring the same thing.
        self.assertEqual(
            inspect.signature(main.derive_output_rate).parameters["window_s"].default,
            main.METRICS_OUTPUT_RATE_WINDOW_S,
        )


DESTINATION_ID = 4242


class FakeClock:
    """Stands in for `main.time` so a four-second window fits in a fast test.

    Patched over main's own reference rather than over time.monotonic itself:
    the event loop reads the clock through its own import, and handing the loop
    a clock that leaps a minute forward between progress blocks would be a
    different experiment than the one being run.
    """

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __getattr__(self, name: str):
        # Anything else main asks of `time` is still the real thing.
        return getattr(time, name)


class FakeProgressStdout:
    """The worker's stdout pipe, one -progress block per tick of the clock.

    _read_progress only ever async-iterates its argument, so byte lines are the
    whole contract -- and advancing the clock from inside the iterator is what
    makes a multi-second lookback testable without sleeping through it.
    """

    def __init__(self, clock: FakeClock, blocks: list[dict[str, str]], gap: float = 1.0) -> None:
        self._clock = clock
        self._blocks = list(blocks)
        self._gap = gap
        self._lines: list[bytes] = []

    def __aiter__(self) -> "FakeProgressStdout":
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            if not self._blocks:
                raise StopAsyncIteration
            block = self._blocks.pop(0)
            self._clock.advance(self._gap)
            self._lines = [f"{key}={value}\n".encode() for key, value in block.items()]
            # ffmpeg terminates every block with progress=, and nothing is
            # parsed until it arrives.
            self._lines.append(b"progress=continue\n")
        return self._lines.pop(0)


def progress_blocks(
    totals: list[int | None], gap: float = 1.0, bitrate: str = "1.0kbits/s"
) -> list[dict[str, str]]:
    """Blocks as ffmpeg spells them, from a cumulative byte schedule.

    `bitrate` is present in every block and is deliberately nonsense: if
    anything downstream ever reaches for ffmpeg's cumulative field again, the
    expected numbers in these tests stop matching immediately.
    """
    blocks = []
    for index, total in enumerate(totals, start=1):
        blocks.append({
            "frame": str(index * 60),
            "fps": "60.0",
            "bitrate": bitrate,
            "total_size": "N/A" if total is None else str(int(total)),
            "out_time_us": str(int(index * gap * 1_000_000)),
            "speed": "1.00x",
            "drop_frames": "0",
            "dup_frames": "0",
        })
    return blocks


class WorkerRateReportingTest(unittest.IsolatedAsyncioTestCase):
    """What FORWARDING / Relay -> Platforms actually shows, end to end.

    Drives the real stdout reader with real -progress text and reads the real
    /api/state snapshot; only the clock and the pipe are stand-ins. No
    subprocess is spawned and no background manager is started, so nothing here
    touches a live worker.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        patcher = mock.patch.object(main, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = main.WorkerManager()
        self.manager._reset_metrics(DESTINATION_ID)

    async def feed(
        self,
        totals: list[int | None],
        gap: float = 1.0,
        read_gap: float | None = None,
        **kwargs,
    ) -> None:
        """`gap` is ffmpeg's own cadence; `read_gap` is how fast we drain it.

        They differ only when modelling a reader that fell behind and then
        drained a backlog in one wakeup.
        """
        stream = FakeProgressStdout(
            self.clock, progress_blocks(totals, gap, **kwargs), gap if read_gap is None else read_gap
        )
        await self.manager._read_progress(stream, DESTINATION_ID)

    def rate(self) -> float | None:
        return self.manager.snapshot(DESTINATION_ID)["bitrate_kbps"]

    async def test_the_panel_shows_the_rate_of_the_last_few_seconds(self) -> None:
        await self.feed([t * STEADY_BYTES_PER_SECOND for t in range(1, 10)])
        snapshot = self.manager.snapshot(DESTINATION_ID)
        self.assertAlmostEqual(snapshot["bitrate_kbps"], STEADY_KBPS)
        # The raw counter and the "is it keeping up" signal are untouched by
        # this change and still come straight off the last block.
        self.assertEqual(snapshot["total_bytes"], 9 * STEADY_BYTES_PER_SECOND)
        self.assertAlmostEqual(snapshot["speed"], 1.0)
        self.assertEqual(snapshot["sample_age_s"], 0.0)
        self.assertEqual(snapshot["uptime_s"], 9.0)
        # The sparkline is the same derived number, so it cannot drift into
        # being a different measurement than the figure printed beside it.
        self.assertEqual(set(snapshot["series"]), {STEADY_KBPS})

    async def test_a_wedged_forwarder_stops_claiming_a_rate(self) -> None:
        """A derived rate is only true while blocks keep arriving.

        If ffmpeg's output write wedges, _read_progress simply stops and the
        last good value would otherwise sit on screen labelled "right now" until
        the stall watchdog fires. Showing the last known rate for a forwarder
        that has gone quiet is the same lie as the lifetime average, just with a
        shorter fuse, so the snapshot has to expire it.
        """
        await self.feed([t * STEADY_BYTES_PER_SECOND for t in range(1, 10)])
        self.assertAlmostEqual(self.rate(), STEADY_KBPS)

        # Still inside the window the rate was measured over: the reading stands.
        self.clock.advance(main.METRICS_OUTPUT_RATE_WINDOW_S)
        self.assertAlmostEqual(self.rate(), STEADY_KBPS)

        # Past it, with no block having arrived, the honest answer is no answer.
        self.clock.advance(main.METRICS_OUTPUT_RATE_WINDOW_S + 0.1)
        snapshot = self.manager.snapshot(DESTINATION_ID)
        self.assertIsNone(snapshot["bitrate_kbps"])
        # The age keeps climbing so the dashboard can say why it went blank, and
        # the raw counter is still the last thing ffmpeg actually reported.
        self.assertGreater(snapshot["sample_age_s"], main.METRICS_OUTPUT_RATE_WINDOW_S)
        self.assertEqual(snapshot["total_bytes"], 9 * STEADY_BYTES_PER_SECOND)

    async def test_a_backlog_read_in_one_wakeup_does_not_print_a_spike(self) -> None:
        """Blocks are stamped when read, not when ffmpeg emitted them.

        A reader that falls behind and then drains several seconds of counters in
        one wakeup would stamp them milliseconds apart; differencing across that
        sliver reports a rate many times the truth.
        """
        # ffmpeg emitted these one second apart as usual; we only got to read
        # them all in a single wakeup, milliseconds apart.
        await self.feed(
            [t * STEADY_BYTES_PER_SECOND for t in range(1, 12)], read_gap=0.001
        )
        rate = self.rate()
        self.assertIsNotNone(rate)
        # Timestamping on arrival would have differenced ten seconds of bytes
        # across ten milliseconds and printed six figures.
        self.assertAlmostEqual(rate, STEADY_KBPS)

    async def test_a_run_that_began_on_the_slate_never_shows_the_lifetime_average(self) -> None:
        """The bug as the operator saw it, through the real reader.

        Fifty-five seconds of failover slate, then the publisher comes back.
        ffmpeg's own bitrate= would still read under 600 kbps a full five
        seconds later, and would go on creeping upward for minutes.
        """
        totals = []
        carried = 0
        for second in range(1, 61):
            carried += SLATE_BYTES_PER_SECOND if second <= 55 else STEADY_BYTES_PER_SECOND
            totals.append(carried)
        await self.feed(totals)

        snapshot = self.manager.snapshot(DESTINATION_ID)
        lifetime_kbps = snapshot["total_bytes"] * 8 / snapshot["uptime_s"] / 1000
        self.assertAlmostEqual(snapshot["bitrate_kbps"], STEADY_KBPS)
        self.assertGreater(snapshot["bitrate_kbps"], lifetime_kbps * 5)
        # The series tells the same story over time rather than only at the
        # end: it sat at the slate's real 100 kbps throughout the outage and
        # then climbed to the real rate, instead of the monotonic creep a
        # lifetime average produces.
        self.assertEqual(snapshot["series"][0], SLATE_KBPS)
        self.assertEqual(snapshot["series"][-1], STEADY_KBPS)

    async def test_a_rate_that_drops_is_reflected_within_the_window(self) -> None:
        """A lifetime average can only ever crawl toward a change. Ten seconds
        at 6000 kbps then four at 3000 must read 3000, not the 5143 kbps the
        average would still be showing."""
        totals = [t * STEADY_BYTES_PER_SECOND for t in range(1, 11)]
        for _ in range(4):
            totals.append(totals[-1] + STEADY_BYTES_PER_SECOND // 2)
        await self.feed(totals)

        snapshot = self.manager.snapshot(DESTINATION_ID)
        self.assertAlmostEqual(snapshot["bitrate_kbps"], STEADY_KBPS / 2)
        lifetime_kbps = snapshot["total_bytes"] * 8 / snapshot["uptime_s"] / 1000
        self.assertAlmostEqual(lifetime_kbps, 5142.857, places=2)
        self.assertNotAlmostEqual(snapshot["bitrate_kbps"], lifetime_kbps, places=1)

    async def test_the_tee_muxer_reports_no_rate_rather_than_a_wrong_one(self) -> None:
        """YouTube destinations run through tee to reach primary and backup, and
        tee reports total_size as N/A, so there is nothing to difference. A
        blank is correct. Falling back to ffmpeg's cumulative bitrate=, to the
        input rate, or to speed would each put a number there that does not
        mean what the label says -- which is the bug, not the fix.
        """
        await self.feed([None] * 30, bitrate="5967.0kbits/s")

        snapshot = self.manager.snapshot(DESTINATION_ID)
        self.assertIsNone(snapshot["bitrate_kbps"])
        self.assertIsNone(snapshot["total_bytes"])
        self.assertEqual(snapshot["series"], [])
        # Running longer must not eventually produce one either.
        await self.feed([None] * 30, bitrate="5967.0kbits/s")
        self.assertIsNone(self.rate())
        self.assertEqual(self.manager.metrics[DESTINATION_ID]["series"], deque())
        self.assertEqual(len(self.manager.metrics[DESTINATION_ID]["rate_history"]), 0)
        # The signals that do work for tee are still reported: speed is the
        # real "is it keeping up" answer, and frames is what promotes the
        # destination to forwarding when there are no bytes to count.
        self.assertAlmostEqual(self.manager.snapshot(DESTINATION_ID)["speed"], 1.0)
        self.assertEqual(self.manager.snapshot(DESTINATION_ID)["frames"], 30 * 60)

    async def test_a_restart_is_never_differenced_against_the_dead_process(self) -> None:
        """A worker restart resets ffmpeg's byte counter to zero while the
        clock keeps running. Carrying the old readings across that boundary
        would difference a fresh counter against a stale one and report a rate
        no process ever pushed."""
        await self.feed([t * SLATE_BYTES_PER_SECOND for t in range(1, 6)])
        self.assertAlmostEqual(self.rate(), SLATE_KBPS)

        # What the retry loop does before spawning the replacement.
        self.manager._reset_metrics(DESTINATION_ID)
        self.assertIsNone(self.rate())
        self.assertEqual(len(self.manager.metrics[DESTINATION_ID]["rate_history"]), 0)
        self.assertEqual(self.manager.snapshot(DESTINATION_ID)["series"], [])

        # The retry backoff -- WORKER_RETRY_MIN_SECONDS, the short one, because
        # a long one would be trimmed out of the history anyway and prove
        # nothing -- then a replacement process whose counter starts over. Its
        # first reading is a position, not a rate: differenced against the dead
        # worker's readings it would have come out around 1290 kbps, which is
        # neither the rate that just ended nor the one about to start.
        self.clock.advance(main.WORKER_RETRY_MIN_SECONDS)
        await self.feed([STEADY_BYTES_PER_SECOND])
        self.assertIsNone(self.rate())

        await self.feed([t * STEADY_BYTES_PER_SECOND for t in range(2, 7)])
        self.assertAlmostEqual(self.rate(), STEADY_KBPS)

    async def test_a_destination_with_no_worker_reports_the_same_shape(self) -> None:
        """/api/state calls snapshot(-1) for every disabled destination, so the
        empty answer has to be the same set of fields the dashboard already
        renders -- with no rate in it."""
        await self.feed([t * STEADY_BYTES_PER_SECOND for t in range(1, 10)])
        live = self.manager.snapshot(DESTINATION_ID)
        empty = self.manager.snapshot(-1)
        self.assertEqual(set(empty), set(live))
        self.assertIsNone(empty["bitrate_kbps"])
        self.assertEqual(empty["series"], [])

    def test_the_worker_clears_its_metrics_before_every_attempt(self) -> None:
        """The restart test above assumes the reset actually happens on the
        retry path, and it has to happen before the new process can report
        anything."""
        source = " ".join(inspect.getsource(main.WorkerManager._run).split())
        reset = source.find("self._reset_metrics(destination_id)")
        spawn = source.find("create_subprocess_exec")
        self.assertNotEqual(reset, -1, "the worker no longer resets its metrics")
        self.assertNotEqual(spawn, -1)
        self.assertLess(reset, spawn)


class SlateRecipeTest(unittest.TestCase):
    """The failover screen must reproduce the feed it replaces, per stream."""

    def test_no_b_frames_and_a_keyframe_interval_matching_the_feed(self) -> None:
        profile = dict(main.DEFAULT_CONTRIBUTION, fps=48.0, gop_seconds=2.0)
        args = joined(main.slate_encode_args(profile))
        # B-frames change the parameter sets and make PTS != DTS, which the FLV
        # muxer rejects outright.
        self.assertIn("-bf 0", args)
        self.assertIn("-g 96", args)          # 2 s at 48 fps
        self.assertIn("-keyint_min 96", args)
        # Scene-cut IDRs would break the fixed cadence the slate exists to keep.
        self.assertIn("-sc_threshold 0", args)

    def test_keyframe_interval_follows_frame_rate(self) -> None:
        """A fixed frame count would be the wrong duration at another fps."""
        for fps, gop in ((30.0, 60), (48.0, 96), (60.0, 120), (59.94, 120)):
            args = joined(main.slate_encode_args(
                dict(main.DEFAULT_CONTRIBUTION, fps=fps, gop_seconds=2.0)))
            self.assertIn(f"-g {gop}", args, f"{fps} fps")

    def test_resolution_and_rate_come_from_the_stream(self) -> None:
        profile = dict(main.DEFAULT_CONTRIBUTION, width=2560, height=1440, fps=30.0)
        graph = main.slate_video_filter(profile)
        self.assertIn("scale=2560:1440", graph)
        self.assertIn("pad=2560:1440", graph)
        self.assertIn("fps=30.0", graph)

    def test_parameter_set_fields_are_taken_from_the_profile(self) -> None:
        """Every field that lands in the SPS or PPS has to agree with the feed."""
        profile = dict(main.DEFAULT_CONTRIBUTION, refs=3, level=51,
                       profile="main", sar="4/3")
        args = joined(main.slate_encode_args(profile))
        self.assertIn("ref=3", args)
        self.assertIn("weightp=0", args)
        self.assertIn("sar=4/3", args)
        self.assertIn("-profile:v main", args)
        self.assertIn("-level:v 5.1", args)

    def test_colour_signalling_follows_the_feed(self) -> None:
        full = joined(main.slate_encode_args(
            dict(main.DEFAULT_CONTRIBUTION, color_range="pc")))
        self.assertIn("fullrange=on", full)
        limited = joined(main.slate_encode_args(main.DEFAULT_CONTRIBUTION))
        self.assertIn("fullrange=off", limited)

    def test_audio_matches_a_stock_obs_track(self) -> None:
        args = joined(main.slate_encode_args(main.DEFAULT_CONTRIBUTION))
        for expected in ("-profile:a aac_low", "-ar 48000", "-ac 2"):
            self.assertIn(expected, args)


class ContributionFingerprintTest(unittest.TestCase):
    def test_a_changed_feed_shape_invalidates_stored_screens(self) -> None:
        base = main.contribution_fingerprint(main.DEFAULT_CONTRIBUTION)
        self.assertEqual(base, main.contribution_fingerprint(dict(main.DEFAULT_CONTRIBUTION)))
        for change in ({"fps": 30.0}, {"width": 1280}, {"gop_seconds": 1.25},
                       {"refs": 3}, {"sar": "4/3"}):
            self.assertNotEqual(
                base,
                main.contribution_fingerprint(dict(main.DEFAULT_CONTRIBUTION, **change)),
                f"{change} should invalidate stored screens",
            )

    def test_fingerprint_is_stable_regardless_of_key_order(self) -> None:
        shuffled = {k: main.DEFAULT_CONTRIBUTION[k]
                    for k in reversed(list(main.DEFAULT_CONTRIBUTION))}
        self.assertEqual(main.contribution_fingerprint(main.DEFAULT_CONTRIBUTION),
                         main.contribution_fingerprint(shuffled))


class ContributionProfileTest(unittest.TestCase):
    """Defaults must be safe for any streamer, not tuned to one setup."""

    def test_defaults_are_platform_standard_not_operator_specific(self) -> None:
        # Two seconds is what Twitch and YouTube both specify.
        self.assertEqual(main.DEFAULT_CONTRIBUTION["gop_seconds"], 2.0)
        self.assertEqual(main.DEFAULT_CONTRIBUTION["bframes"], 0)

    def test_profile_names_map_to_values_x264_accepts(self) -> None:
        for reported, expected in (("High", "high"), ("Main", "main"),
                                   ("Constrained Baseline", "baseline"),
                                   ("High 4:2:2", "high422"),
                                   ("High 4:4:4 Predictive", "high444"),
                                   ("Something Exotic", "high")):
            key = reported.lower().replace(" ", "").replace(":", "")
            self.assertEqual(main.X264_PROFILES.get(key, "high"), expected, reported)

    def test_frame_rate_parsing_handles_rationals(self) -> None:
        self.assertAlmostEqual(main._parse_rate("60000/1001"), 59.9400599, places=5)
        self.assertEqual(main._parse_rate("48/1"), 48.0)
        self.assertIsNone(main._parse_rate("0/0"))
        self.assertIsNone(main._parse_rate(None))


class TeardownIntentTest(unittest.TestCase):
    """A restart and a stop must look different to the platform.

    SIGTERM lets FFmpeg run its normal shutdown, and the RTMP muxer then sends
    FCUnpublish/deleteStream -- the same thing OBS sends when a streamer presses
    Stop Streaming. Twitch reads that as a deliberate end: new stream id, split
    VOD, reset viewer count. A restart must instead look like a brief drop.
    """

    def setUp(self) -> None:
        self.manager = main.WorkerManager()

    def test_restart_requests_an_abrupt_teardown(self) -> None:
        self.assertIn("abrupt=True", inspect.getsource(main.restart_destination))

    def test_genuine_stops_keep_the_clean_unpublish(self) -> None:
        # Turning a destination off, deleting it, or suspending its owner all
        # mean the broadcast really is over on that platform.
        for route in (main.toggle_destination, main.delete_destination,
                      main.set_team_member_status):
            self.assertNotIn("abrupt=True", inspect.getsource(route), route.__name__)

    def test_shutdown_is_graceful(self) -> None:
        self.assertNotIn("abrupt=True", inspect.getsource(main.WorkerManager.shutdown))

    def test_cancel_handler_branches_on_the_flag(self) -> None:
        source = inspect.getsource(main.WorkerManager._run)
        self.assertIn("if destination_id in self.abrupt:", source)
        # The abrupt branch must not send SIGTERM first.
        abrupt = source.split("if destination_id in self.abrupt:")[1].split("else:")[0]
        self.assertIn("process.kill()", abrupt)
        self.assertNotIn("terminate()", abrupt)

    def test_flag_starts_clear_and_is_scoped_per_destination(self) -> None:
        self.assertEqual(self.manager.abrupt, set())
        self.manager.abrupt.add(7)
        self.assertNotIn(9, self.manager.abrupt)


class MediaStatusTest(unittest.TestCase):
    def test_unreachable_media_server_is_not_reported_as_an_outage(self) -> None:
        """`known` is what stops a transport hiccup from firing an outage ad."""
        self.assertFalse(main._path_state(None)["online"])
        self.assertTrue(main._path_state(None)["known"])

    def test_online_means_a_publisher_not_merely_a_readable_path(self) -> None:
        """An always-available path reports ready=true forever, because the
        slate can always be read. Only `online` tracks the real publisher."""
        idle = main._path_state({"name": "studio", "ready": True, "online": False})
        self.assertTrue(idle["available"])
        self.assertFalse(idle["online"])
        live = main._path_state({"name": "studio", "ready": True, "online": True, "tracks": ["H264"]})
        self.assertTrue(live["online"])


class FastFailoverEnforcementTest(unittest.IsolatedAsyncioTestCase):
    """The stall kick: fires only on evidence, never on absence of evidence."""

    STREAM = {"id": 7, "slug": "studio"}

    def manager(self) -> main.FailoverAdManager:
        return main.FailoverAdManager()

    async def enforce(self, *, reachable: bool, stalled: float | None,
                      manager: main.FailoverAdManager | None = None) -> mock.AsyncMock:
        kick = mock.AsyncMock()
        with mock.patch.object(main.signal_metrics, "reachable", reachable), \
                mock.patch.object(main.signal_metrics, "stalled_for", return_value=stalled), \
                mock.patch.object(main, "fast_failover_enabled", return_value=True), \
                mock.patch.object(main, "current_program_mode", return_value="live"), \
                mock.patch.object(main, "kick_stream_publishers", kick):
            await (manager or self.manager())._enforce_fast_failover(self.STREAM)
        return kick

    async def test_an_unreachable_media_server_is_not_a_stall(self) -> None:
        """A failed sample leaves the metrics snapshot stale rather than empty,
        so stalled_for keeps growing during a MediaMTX outage. Kicking on that
        would drop a publisher that may be fine — hold, exactly as the outage
        state machine does."""
        kick = await self.enforce(reachable=False, stalled=999.0)
        kick.assert_not_awaited()

    async def test_a_gap_srt_can_still_recover_is_left_alone(self) -> None:
        kick = await self.enforce(
            reachable=True, stalled=main.FAST_FAILOVER_STALL_SECONDS - 0.1
        )
        kick.assert_not_awaited()

    async def test_a_real_stall_is_kicked_once_not_hammered(self) -> None:
        manager = self.manager()
        first = await self.enforce(
            reachable=True, stalled=main.FAST_FAILOVER_STALL_SECONDS + 0.5, manager=manager
        )
        first.assert_awaited_once_with("studio")
        # The 1 s check loop will see the same stall again next tick; the
        # dedupe window keeps that from turning into a kick storm.
        second = await self.enforce(
            reachable=True, stalled=main.FAST_FAILOVER_STALL_SECONDS + 1.5, manager=manager
        )
        second.assert_not_awaited()

    def test_the_threshold_stays_above_srt_recovery(self) -> None:
        """The tsbpd window is 500 ms: gaps up to ~1 s are refilled by
        retransmission and never reach a viewer. A threshold at or below that
        trades an invisible blip for a forced OBS reconnect."""
        self.assertGreaterEqual(main.FAST_FAILOVER_STALL_SECONDS, 1.5)
        self.assertLessEqual(main.FAST_FAILOVER_CHECK_SECONDS, main.METRICS_INTERVAL_SECONDS)


class StallLedgerTest(unittest.TestCase):
    """Recovered stalls are recorded when they happen, because the question
    'how often does the link stall for 1-1.5 s?' cannot be answered after the
    fact from anything else the system keeps."""

    def record_at(self, metrics: main.SignalMetrics, moment: float, received: int) -> None:
        metrics.paths = {"studio": {"inboundBytes": received}}
        with mock.patch.object(main.time, "monotonic", return_value=moment):
            metrics._record("studio")

    def fresh_metrics(self) -> main.SignalMetrics:
        metrics = main.SignalMetrics()
        metrics.publishers = {"studio": {"id": "conn-1"}}
        return metrics

    def test_a_recovered_stall_is_logged_with_its_duration(self) -> None:
        metrics = self.fresh_metrics()
        self.record_at(metrics, 10.0, 100)
        self.record_at(metrics, 11.0, 200)   # moving normally
        self.record_at(metrics, 12.0, 200)   # stall begins
        with self.assertLogs("relay", level="INFO") as captured:
            self.record_at(metrics, 12.3, 300)
        self.assertIn("stalled 1.3s then recovered", captured.output[0])

    def test_a_sub_second_hiccup_is_not_written_up(self) -> None:
        metrics = self.fresh_metrics()
        self.record_at(metrics, 10.0, 100)
        self.record_at(metrics, 10.9, 200)
        with self.assertNoLogs("relay", level="INFO"):
            self.record_at(metrics, 11.8, 300)

    def test_a_sampling_outage_is_not_mistaken_for_a_stall(self) -> None:
        """If MediaMTX could not be sampled for a while, bytes advance a lot on
        the next successful read; that is our blindness, not their stall."""
        metrics = self.fresh_metrics()
        self.record_at(metrics, 10.0, 100)
        self.record_at(metrics, 11.0, 200)
        with self.assertNoLogs("relay", level="INFO"):
            self.record_at(metrics, 16.0, 900)  # 5 s since the last sample


if __name__ == "__main__":
    unittest.main()
