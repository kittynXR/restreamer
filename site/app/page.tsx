"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

type Session = { setup_required: boolean; authenticated: boolean; csrf?: string | null; user?: User | null };
type User = { username: string; display_name: string; role: string };
// bitrate_kbps is the rate going out right now: the router derives it from how
// many bytes actually moved over the last few seconds. It is deliberately not
// ffmpeg's own `bitrate=`, which is total bytes over the whole life of the
// process and reads minutes low after any dropout. null is a real answer here —
// see outputRate() — and so is a short `series`: the router only appends a
// sample for a span it could honestly measure, so the line can be shorter than
// the uptime implies, or empty for a destination whose muxer reports no byte
// count at all.
type OutputMetrics = {
  bitrate_kbps?: number | null;
  speed?: number | null;
  frames?: number | null;
  total_bytes?: number | null;
  uptime_s?: number | null;
  sample_age_s?: number | null;
  series: number[];
};
type SignalMetrics = {
  known: boolean;
  publishing: boolean;
  rtt_ms?: number | null;
  receive_mbps?: number | null;
  link_capacity_mbps?: number | null;
  loss_rate?: number | null;
  packets_lost?: number | null;
  packets_retransmitted?: number | null;
  packets_dropped?: number | null;
  packets_belated?: number | null;
  receive_buffer_ms?: number | null;
  latency_ms?: number | null;
  frames_in_error?: number | null;
  bytes_received?: number | null;
  online_since?: string | null;
  reader_count?: number;
  track_summary: string[];
  stalled_for_s?: number | null;
  series: { bitrate_kbps: number[]; rtt_ms: number[] };
};
// music_fallback arrives as sqlite's 0/1, not a JSON boolean — the same shape
// `enabled` already comes back in. Never read it with a bare truthiness test:
// absent means "the router's default for this platform", which is now on for
// YouTube and X. Go through musicFallbackOn().
type Destination = { id: number; name: string; platform: string; enabled: number; state: string; last_error?: string | null; restart_count?: number; last_started_at?: string | null; music_fallback?: number; metrics?: OutputMetrics };
type MediaTrack = { codec?: string; codecProps?: { sampleRate?: number } };
type TwitchEvent = { checked_at: string; preroll_before?: number | null; requested_length?: number | null; status: string; message?: string | null };
type TwitchState = { available: boolean; connected: boolean; login?: string | null; failover_ads_enabled: boolean; grace_seconds: number; last_event?: TwitchEvent | null };
type ScreenAsset = { kind: string; status: "missing" | "converting" | "ready" | "error"; original_name?: string | null; message?: string | null; updated_at?: string | null; stale?: boolean };
type StreamState = {
  user: User;
  csrf: string;
  stream: {
    slug: string;
    obs_url: string;
    media: { known?: boolean; available: boolean; online: boolean; tracks: string[]; tracks2?: MediaTrack[]; bytes_received?: number };
    signal: SignalMetrics;
    fast_failover: boolean;
    fast_failover_seconds: number;
    ultra_failover: boolean;
    ultra_failover_seconds: number;
    stalls?: { window_days: number; over_half_s: number; over_1s: number; longest_s?: number | null };
  };
  twitch_ingest: { name: string; latency_ms?: number | null; checked_at?: string | null };
  twitch: TwitchState;
  screens: { mode: "brb" | "starting_soon"; program_mode: "live" | "brb" | "starting_soon"; brb: ScreenAsset; starting_soon: ScreenAsset };
  destinations: Destination[];
};
type TeamMember = { id:number; username:string; display_name:string; role:string; enabled:boolean; created_at:string; slug:string; destination_count:number; enabled_destination_count:number; forwarding_count?:number; twitch_connected:boolean; online?:boolean; receive_mbps?:number|null };
type TeamInvite = { id:number; label:string; created_at:string; expires_at:string };
type TeamState = { members:TeamMember[]; invitations:TeamInvite[] };
type InviteInfo = { label:string; invited_by:string; expires_at:string };
type InviteResult = { id:number; invite_url:string; expires_at:string };

function formatNumber(value: number | null | undefined, digits = 0, suffix = ""): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toFixed(digits) + suffix;
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return "—";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${total % 60}s`;
  return `${total}s`;
}

function formatBytes(value: number | null | undefined): string {
  if (value == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(size >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

/** A compact trend line. Never the only carrier of a value — always paired with
 *  the numeral it summarises, so it can stay hidden from assistive tech.
 *
 *  Anything that is not a finite number is dropped rather than plotted: a
 *  missing sample is not a zero, and a line dragged down to the floor would
 *  claim nothing is going out when the truth is only that Relay could not
 *  measure it. With nothing left to draw, say so with a dash instead of
 *  returning an empty box — a blank cell looks like a rendering fault, and one
 *  of these cells is permanently empty for any destination whose muxer never
 *  reports an output byte count. */
function Spark({ points, tone = "green", height = 30 }: { points: (number | null | undefined)[]; tone?: "green" | "amber"; height?: number }) {
  const width = 120;
  const values = points.filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  if (values.length < 2) return <span className="spark spark-empty" aria-hidden="true">—</span>;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const y = (value: number) => height - 2 - ((value - min) / span) * (height - 4);
  const x = (index: number) => (index / (values.length - 1)) * width;
  const line = values.map((value, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(value).toFixed(1)}`).join("");
  const area = `${line}L${width},${height}L0,${height}Z`;
  return (
    <svg className={`spark ${tone}`} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
      <path className="spark-area" d={area} />
      <path className="spark-line" d={line} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
/** Four states, not two: "retrying" for an hour must not look like "connecting"
 *  two seconds ago. */
function destinationStatus(destination: Destination): { label: string; tone: string } {
  if (!destination.enabled) return { label: "Off", tone: "" };
  if (destination.state === "forwarding") return { label: "Forwarding", tone: "online" };
  if (destination.state === "retrying") return { label: "Not connecting", tone: "failed" };
  if (destination.state === "error") return { label: "Stopped", tone: "failed" };
  return { label: "Connecting", tone: "pending" };
}

function videoSummary(signal: SignalMetrics): string {
  return signal.track_summary.find((track) => /\d+×\d+/.test(track)) || "";
}

const AUDIO_CODEC_HINTS = ["audio", "aac", "opus", "ac-3", "g711", "lpcm", "mp3", "vorbis"];

/** Mirrors audio_track_count() in router/app/main.py. This count is what the
 *  forwarders degrade on when fewer than two audio tracks arrive, so counting
 *  them any other way here would let the dashboard promise a Track 2 that
 *  nothing is mapping. null means "not known yet", not none: MediaMTX's flat
 *  `tracks` list mixes video and audio and can never be counted directly. */
function audioTrackCount(media: StreamState["stream"]["media"]): number | null {
  if (media.known === false || !media.available) return null;
  const sounds = (value: string) => AUDIO_CODEC_HINTS.some((hint) => value.toLowerCase().includes(hint));
  const detailed = media.tracks2 || [];
  if (detailed.length) return detailed.filter((track) => track.codecProps?.sampleRate || sounds(String(track.codec || ""))).length;
  if (!media.tracks.length) return null;
  return media.tracks.filter((track) => sounds(String(track))).length;
}

function audioSummary(media: StreamState["stream"]["media"]): string {
  const count = audioTrackCount(media);
  if (count == null) return media.known === false ? "Unknown" : "No tracks yet";
  return `${count} track${count === 1 ? "" : "s"}`;
}

function signalHealth(signal: SignalMetrics, online: boolean): { label: string; tone: string } {
  if (!signal.known) return { label: "Unknown", tone: "disabled" };
  if (!online) return { label: "Offline", tone: "disabled" };
  const loss = signal.loss_rate ?? 0;
  const rtt = signal.rtt_ms ?? 0;
  if (loss > 1 || rtt > 200) return { label: "Degraded", tone: "disabled" };
  return { label: "Healthy", tone: "armed" };
}

type AudioRoute = { platform: string; label: string; maps: string; note: string; why: string };

/** The user-facing copy of build_audio_args() in router/app/main.py — change
 *  both together. Routing is a property of the platform, never a setting: Relay
 *  never mixes and never re-encodes, so the only question left is which of the
 *  two OBS tracks a platform is allowed to hear.
 *
 *  `why` is only used where someone is actually choosing a platform — the Add
 *  destination dialog. The reference table states the routing and stops. */
const audioRoutes: AudioRoute[] = [
  { platform: "twitch", label: "Twitch", maps: "Tracks 1 + 2", note: "live plus VOD", why: "Track 1 is what live viewers hear. Track 2 travels alongside it as the Twitch VOD track, so the saved video has no music." },
  { platform: "youtube", label: "YouTube", maps: "Track 2", note: "clean, no music", why: "YouTube scans the archive for music, so it only ever receives the clean mix." },
  { platform: "x", label: "X", maps: "Track 2", note: "clean, no music", why: "X publishes the replay automatically — and the music is a reason to come to Twitch." },
  { platform: "rplay", label: "RPLAY", maps: "Track 1", note: "full live mix", why: "Nothing stays published afterwards, so RPLAY hears exactly what Twitch viewers hear." },
  { platform: "custom", label: "Custom RTMP", maps: "Track 1", note: "full live mix", why: "The full live experience is the least surprising thing to send somewhere Relay knows nothing about." },
];

/** An unrecognised platform lands on the custom row, matching the router's own
 *  final else branch. */
function audioRouteFor(platform: string): AudioRoute {
  return audioRoutes.find((route) => route.platform === platform) || audioRoutes[audioRoutes.length - 1];
}

function audioRouteLabel(platform: string): string {
  const route = audioRouteFor(platform);
  return `${route.maps} · ${route.note}`;
}

/** Mirrors the `clean_track_only` set in build_audio_args(). These two are the
 *  only platforms with a clean track to lose, so they are the only ones where
 *  the fallback is a real choice — everything else maps Track 1 either way, and
 *  the router returns 422 rather than storing a setting that changes nothing. */
function hasMusicFallbackChoice(platform: string): boolean {
  return platform === "youtube" || platform === "x";
}

/** Mirrors resolve_music_fallback() in router/app/main.py. A missing flag is
 *  not "off" — it means the router would fill in its own default, which is on
 *  for the two clean-track platforms and off everywhere else. Reading the raw
 *  0/1 with Boolean() would draw a muted destination that is in fact set to
 *  carry Track 1, which is the one belief this whole control exists to keep
 *  accurate. */
function musicFallbackOn(destination: Destination): boolean {
  if (destination.music_fallback == null) return hasMusicFallbackChoice(destination.platform);
  return Boolean(destination.music_fallback);
}

/** What a destination is mapping *right now*, which is not always what its
 *  platform row promises. The forwarders degrade every destination when fewer
 *  than two tracks arrive, so repeating the normal routing during a one-track
 *  publish would claim a Track 2 that nothing is actually sending. */
function effectiveAudio(destination: Destination, arriving: number | null): { label: string; tone: "" | "degraded" | "risk" } {
  const described = describeAudio(destination, arriving);
  // Colour marks something happening, never something forecast. A destination
  // that is off is carrying nothing, so tinting it red is the permanent
  // alarm-with-nothing-to-act-on this panel exists to avoid. The label still
  // says what it would send, quietly. This pairs the row with TrackTwoAlert,
  // which is gated on the same condition.
  return destination.enabled ? described : { ...described, tone: "" };
}

function describeAudio(destination: Destination, arriving: number | null): { label: string; tone: "" | "degraded" | "risk" } {
  const normal = audioRouteLabel(destination.platform);
  // null is "not known yet", not "none" — assume the documented layout, exactly
  // as the router does when the worker starts before OBS connects.
  if (arriving == null || arriving >= 2) return { label: normal, tone: "" };
  if (arriving === 0) return { label: "Muted (OBS is sending no audio)", tone: "degraded" };
  if (hasMusicFallbackChoice(destination.platform)) {
    // Relay fixes the mapping when a forwarder starts and this PATCH deliberately
    // does not restart it, so for a running destination the stored choice and the
    // live `-map` set can disagree in BOTH directions. Say what it is set to,
    // never what it is doing. An off destination is a forecast, so it reads
    // plainly.
    if (destination.enabled) {
      return musicFallbackOn(destination)
        ? { label: "Set to send Track 1 — carries your music", tone: "risk" }
        : { label: "Set to mute — applies when this destination restarts", tone: "risk" };
    }
    return musicFallbackOn(destination)
      ? { label: "Track 1 (Track 2 unavailable — carries your music)", tone: "risk" }
      : { label: "Muted (Track 2 unavailable)", tone: "degraded" };
  }
  if (destination.platform === "twitch") return { label: "Track 1 (Track 2 unavailable — no VOD track)", tone: "degraded" };
  return { label: normal, tone: "" };
}

/** The consequence, spelled out at the point of choice rather than hidden in a
 *  tooltip: both of these keep the audio permanently, and both of them scan it. */
function archiveConsequence(platform: string): string {
  return platform === "youtube"
    ? "YouTube runs Content ID over the saved video."
    : "X publishes the replay publicly and keeps it.";
}

/** One-track publish, and only while something is actually forwarding. Nobody
 *  needs an alert about a clean track that no destination is waiting for, so a
 *  page with every output off stays quiet.
 *
 *  It leads with the only real repair — Track 2 in OBS — and stops there. The
 *  per-destination consequences live on the rows, which is where the operator
 *  can act on them; repeating them here is what made this a wall of text.
 *
 *  It never describes what a running forwarder is mapping: Relay picks the
 *  mapping when a forwarder starts, so an enabled destination and its stored
 *  setting can legitimately disagree. */
function TrackTwoAlert({ destinations, arriving }: { destinations: Destination[]; arriving: number | null }) {
  // Only the one-track case. At zero tracks nothing has a mix to choose from.
  if (arriving !== 1) return null;
  if (!destinations.some((destination) => Boolean(destination.enabled))) return null;
  return (
    <div className="track-alert" role="alert">
      <span className="track-alert-mark" aria-hidden="true">!</span>
      <div className="track-alert-body">
        <strong>OBS is sending one audio track</strong>
        <p>Turn on <b>Track 2</b> in OBS under Settings → Output → Advanced and point it at your clean mix. Until it arrives there is no clean track to forward, so each destination below shows what it is sending instead.</p>
      </div>
    </div>
  );
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong>{hint && <small>{hint}</small>}</div>;
}

/** OBS → Relay: the contribution feed's own health, straight from MediaMTX's
 *  SRT connection counters. */
function ContributionPanel({ signal, media, programMode, stalls }: { signal: SignalMetrics; media: StreamState["stream"]["media"]; programMode: string; stalls?: StreamState["stream"]["stalls"] }) {
  const health = signalHealth(signal, media.online);
  const bitrate = signal.series.bitrate_kbps;
  const rtt = signal.series.rtt_ms;
  return (
    <article className="control-card metrics-card">
      <div className="control-card-head"><div><p className="eyebrow">CONTRIBUTION FEED</p><h2>OBS → Relay</h2></div><span className={`status-chip ${health.tone}`}>{health.label}</span></div>
      <div className="metric-row">
        <Metric label="BITRATE" value={formatNumber(signal.receive_mbps != null ? signal.receive_mbps * 1000 : null, 0, " kb/s")} />
        <Metric label="ROUND TRIP" value={formatNumber(signal.rtt_ms, 1, " ms")} />
        <Metric label="PACKET LOSS" value={formatNumber(signal.loss_rate, 3, " %")} />
        <Metric label="BUFFER" value={formatNumber(signal.receive_buffer_ms, 0, " ms")} hint={signal.latency_ms != null ? `${Math.round(signal.latency_ms)} ms latency` : undefined} />
      </div>
      <div className="spark-row">
        <div className="spark-cell"><span>Bitrate</span><Spark points={bitrate} /></div>
        <div className="spark-cell"><span>Round trip</span><Spark points={rtt} tone="amber" /></div>
      </div>
      <dl className="metric-list">
        {/* Counter semantics (gosrt, verified at source): packets_lost counts
            sequences detected missing, almost all of which the retransmissions
            above recover; packets_belated is the part that arrived too late to
            use. packets_dropped is duplicate copies being discarded — SRT
            working, not failing — which is why it is not shown here as loss. */}
        <div><dt>Retransmitted</dt><dd>{formatNumber(signal.packets_retransmitted)} pkts</dd></div>
        <div><dt>Went missing</dt><dd>{formatNumber(signal.packets_lost)} pkts</dd></div>
        <div><dt>Too late to use</dt><dd>{formatNumber(signal.packets_belated)} pkts</dd></div>
        <div><dt>Corrupt frames</dt><dd>{formatNumber(signal.frames_in_error)}</dd></div>
        <div><dt>Received</dt><dd>{formatBytes(signal.bytes_received)}</dd></div>
        <div><dt>Forwarders reading</dt><dd>{formatNumber(signal.reader_count)}</dd></div>
        {/* From the persisted stall ledger, not this connection: delivery gaps
            in this feed after SRT recovery, counted across the whole week. */}
        {stalls && <div><dt>Gaps over 0.5s · {stalls.window_days}d</dt><dd>{formatNumber(stalls.over_half_s)}</dd></div>}
        {stalls && <div><dt>Gaps over 1s · {stalls.window_days}d</dt><dd>{formatNumber(stalls.over_1s)}</dd></div>}
      </dl>
      <p className="metric-caption">
        {signal.track_summary.length ? signal.track_summary.join(" · ")
          : programMode !== "live" ? "A manual screen is on air; OBS is held off."
          : signal.known ? "Waiting for track information from OBS."
          : "Relay cannot reach the media server right now."}
      </p>
    </article>
  );
}

/** What to put where the outgoing rate goes, given that the rate can honestly
 *  be unknown.
 *
 *  A missing rate has two causes that must not look alike on screen: a
 *  forwarder that has not yet run long enough for the router to measure a span
 *  (that fills in within seconds), and a destination whose muxer never reports
 *  how many bytes it has sent (that never fills in). "Measuring…" on something
 *  that will never resolve is a lie told slowly; a stale or zeroed number is a
 *  lie told immediately. Both cases say plainly what is happening and lean on
 *  speed and status, which are unaffected either way.
 *
 *  total_bytes is the discriminator, not a timer: it is the same counter the
 *  rate is differenced from, so its absence is exactly the condition that makes
 *  a rate impossible. The few-second grace only covers the gap before the first
 *  sample of any kind has landed. */
function outputRate(metrics: OutputMetrics | undefined): { text: string; hint?: string; measured: boolean; unreported: boolean } {
  if (!metrics) return { text: "—", measured: false, unreported: false };
  if (metrics.bitrate_kbps != null) {
    return { text: formatNumber(metrics.bitrate_kbps, 0, " kb/s"), hint: "What this forwarder is sending out right now, measured over the last few seconds of traffic.", measured: true, unreported: false };
  }
  if (metrics.total_bytes == null && (metrics.uptime_s ?? 0) >= 5) {
    return { text: "Not reported", hint: "This forwarder is not telling Relay how many bytes it has sent, so there is no rate to measure. Speed and status are unaffected.", measured: false, unreported: true };
  }
  return { text: "Measuring…", hint: "Relay measures the outgoing rate over a few seconds of traffic; this fills in shortly after a forwarder starts.", measured: false, unreported: false };
}

/** Relay → each platform, from ffmpeg's own -progress reporting rather than
 *  from whether the process happens to still be alive. */
function OutputsPanel({ destinations }: { destinations: Destination[] }) {
  const live = destinations.filter((destination) => destination.enabled);
  // Only trust the numbers while the forwarder is actually up: during a retry
  // backoff the last sample is from the dead process, and its uptime would keep
  // climbing.
  const rows = live.map((destination) => {
    const metrics = destination.state === "forwarding" ? destination.metrics : undefined;
    return { destination, metrics, rate: outputRate(metrics) };
  });
  const anyUnreported = rows.some((row) => row.rate.unreported);
  return (
    <article className="control-card metrics-card">
      <div className="control-card-head"><div><p className="eyebrow">FORWARDING</p><h2>Relay → Platforms</h2></div><span className={`status-chip ${live.some((d) => d.state === "forwarding") ? "armed" : "disabled"}`}>{live.filter((d) => d.state === "forwarding").length} of {live.length} up</span></div>
      {rows.length ? <div className="output-list">{rows.map(({ destination, metrics, rate }) => {
        const status = destinationStatus(destination);
        const slow = metrics?.speed != null && metrics.speed < 0.98;
        return (
          <div className="output-row" key={destination.id}>
            <PlatformIcon platform={destination.platform} fallback={destination.name[0]?.toUpperCase()} />
            <div className="output-identity">
              <strong>{destination.name}</strong>
              <span className={`route-status ${status.tone}`}><span />{status.label}</span>
            </div>
            <div className="output-numbers">
              <b className={rate.measured ? undefined : "quiet"} title={rate.hint}>{rate.text}</b>
              {/* "up" is load-bearing: a rate sitting next to a bare duration
                  invites reading the rate as an average over that duration,
                  which is the exact misreading this panel is fixing. */}
              <span className={slow ? "warn" : undefined}>{formatNumber(metrics?.speed, 2, "×")}{metrics && metrics.uptime_s != null ? ` · up ${formatDuration(metrics.uptime_s)}` : ""}</span>
            </div>
            <Spark points={metrics?.series || []} tone={slow ? "amber" : "green"} height={24} />
            {Boolean(destination.restart_count) && <span className="restart-badge" title={`${destination.restart_count} forwarder start${destination.restart_count === 1 ? "" : "s"} in total`}>{destination.restart_count}↻</span>}
          </div>
        );
      })}</div> : <div className="metric-empty">Turn a destination on to see forwarding health.</div>}
      <p className="metric-caption">
        Bitrate is what each forwarder is sending out right now, measured over the last few seconds — not an average since it started. Speed below 1.00× means the forwarder is not keeping up with the incoming feed.
        {anyUnreported ? " “Not reported” means that forwarder gives Relay no byte count to measure, so no rate is shown; its speed and status still tell you whether it is keeping up." : ""}
      </p>
    </article>
  );
}

/** Reference, and nothing more: the answer to "why does YouTube sound
 *  different?" has to be visible without asking anyone. Deliberately quiet —
 *  no warning tone lives here, because a routing rule is never a problem. What
 *  happens when a track is missing is a live condition, so it is raised on the
 *  destination it affects, at the moment it affects it. */
function AudioRoutingPanel({ media }: { media: StreamState["stream"]["media"] }) {
  const arriving = audioTrackCount(media);
  const healthy = arriving != null && arriving >= 2;
  return (
    <section className="audio-routing" id="audio" aria-labelledby="audio-routing-title">
      <div className="section-heading">
        <div><p className="eyebrow">AUDIO ROUTING</p><h2 id="audio-routing-title">Fixed by platform</h2></div>
        <span className={`status-chip ${healthy ? "armed" : "disabled"}`} role="status">{arriving == null ? "Waiting for OBS" : `${arriving} track${arriving === 1 ? "" : "s"} arriving`}</span>
      </div>
      <div className="audio-contract">
        <div><b>Track 1 — full live mix</b><span>Music, game, and voice.</span></div>
        <div><b>Track 2 — clean mix</b><span>Game and voice, no music.</span></div>
      </div>
      <div className="audio-table-wrap">
        <table className="audio-table">
          <thead><tr><th scope="col">Platform</th><th scope="col">Receives</th><th scope="col">Notes</th></tr></thead>
          <tbody>{audioRoutes.map((route) => (
            <tr key={route.platform}>
              <th scope="row"><span className="audio-platform"><PlatformIcon platform={route.platform} fallback="•" />{route.label}</span></th>
              <td className="audio-maps">{route.maps}</td>
              <td>{route.note}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <p className="audio-note">Relay never mixes or re-encodes. Send Tracks 1 and 2 from OBS and each destination gets the right one.</p>
    </section>
  );
}

/** The only user-facing audio setting, and only on YouTube and X. It decides
 *  nothing while both tracks arrive, so it is rendered in exactly one place —
 *  the start-forwarding dialog during a one-track publish, where the operator
 *  is choosing in the moment rather than reading a fieldset that has sat on
 *  every YouTube and X row since the day the destination was added.
 *
 *  Two named outcomes rather than a switch: "on" and "off" say nothing about
 *  what reaches an archive. Track 1 is first because it is the router's own
 *  default for a new destination; muting is the deliberate departure. The
 *  caller supplies the warning and the consequence — this is just the pills. */
function MusicFallbackChoice({ value, busy = false, group, owner, onChoose }: { value: boolean; busy?: boolean; group: string; owner: string; onChoose: (enabled: boolean) => void }) {
  return (
    // `owner` names the destination: the legend alone would announce as an
    // unlabelled "Send instead" group in a dialog that never says which row it
    // belongs to.
    <fieldset className="fallback-choice" disabled={busy} aria-label={`${owner}: what to send while Track 2 is missing`}>
      <legend>Send instead</legend>
      <div className="fallback-options">
        <label className={value ? "chosen" : ""}>
          <input type="radio" name={group} checked={value} onChange={() => onChoose(true)} />
          <span>Track 1 (has your music)</span>
        </label>
        <label className={value ? "" : "chosen"}>
          <input type="radio" name={group} checked={!value} onChange={() => onChoose(false)} />
          <span>No audio</span>
        </label>
      </div>
    </fieldset>
  );
}

const platformMarks: Record<string, { src: string; label: string }> = {
  twitch: { src: "/logos/twitch.svg", label: "Twitch" },
  youtube: { src: "/logos/youtube.svg", label: "YouTube" },
  rplay: { src: "/logos/rplay.png", label: "RPLAY" },
  x: { src: "/logos/x.svg", label: "X" },
};

function PlatformIcon({ platform, fallback = "?" }: { platform: string; fallback?: string }) {
  const mark = platformMarks[platform];
  // These tiny local brand marks do not benefit from image optimization.
  // eslint-disable-next-line @next/next/no-img-element
  return <span className={`platform-icon ${platform}`} aria-label={mark ? `${mark.label} logo` : `${platform} destination`} role="img">{mark ? <img src={mark.src} alt="" /> : fallback}</span>;
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

async function json<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: "same-origin", ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, body.detail || "Something went wrong");
  return body as T;
}

function isSignedOut(err: unknown): boolean {
  // 403 is CSRF or owner scope, not an expired session — only 401 ends it.
  return err instanceof ApiError && err.status === 401;
}

export default function Home() {
  const [session, setSession] = useState<Session | null>(null);
  const [state, setState] = useState<StreamState | null>(null);
  const [error, setError] = useState("");
  const [showObs, setShowObs] = useState(false);
  const [showOnboardingOffer, setShowOnboardingOffer] = useState(false);
  const [showOnboarding, setShowOnboarding] = useState(false);
  const [showMonitor, setShowMonitor] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  const [copied, setCopied] = useState(false);
  const [pendingToggle, setPendingToggle] = useState<Destination | null>(null);
  const [pendingRemove, setPendingRemove] = useState<Destination | null>(null);
  // Only the direction that departs from the default is confirmed, and that is
  // now muting: a new destination arrives set to Track 1 with no dialog at all,
  // so putting a modal in front of choosing the same value would be theatre.
  // Muting is the one that can take an output off the air — Relay publishes it
  // with no audio track, and neither ingest is known to accept that.
  const [pendingMute, setPendingMute] = useState<Destination | null>(null);
  // The id of the destination whose fallback is in flight, so one busy row does
  // not freeze the radios on every other destination.
  const [fallbackBusy, setFallbackBusy] = useState(0);
  // Errors raised inside a dialog must render inside it — the backdrop hides
  // the page-level notice completely.
  const [modalError, setModalError] = useState("");
  const [toggleBusy, setToggleBusy] = useState(false);
  const [pendingAutoArm, setPendingAutoArm] = useState(false);
  const [pendingScreenMode, setPendingScreenMode] = useState<"live" | "brb" | "starting_soon" | null>(null);
  const [protectionBusy, setProtectionBusy] = useState(false);
  const [uploadBusy, setUploadBusy] = useState<"" | "brb" | "starting_soon">("");
  const [inviteToken, setInviteToken] = useState<string | null>(null);
  const [inviteChecked, setInviteChecked] = useState(false);
  const [stateError, setStateError] = useState("");
  const [signedOut, setSignedOut] = useState(false);
  const [staleSince, setStaleSince] = useState(0);
  const onboardingUsername = state?.user.username;
  // Every /api/state write is stamped, so a slow poll can never overwrite the
  // fresher result of a mutation that started after it.
  const generation = useRef(0);

  const applyState = useCallback((next: StreamState, stamp: number) => {
    if (stamp < generation.current) return;
    generation.current = stamp;
    setState(next);
  }, []);

  // A plain incrementing ticket, not a clock: Date.now() is not monotonic, and a
  // backward step would freeze the dashboard on stale data.
  const ticket = useRef(0);

  const refreshState = useCallback(async () => {
    const stamp = ++ticket.current;
    const next = await json<StreamState>("/api/state");
    applyState(next, stamp);
  }, [applyState]);

  const loadSession = useCallback(async () => {
    try {
      const next = await json<Session>("/api/session");
      setSession(next);
      setSignedOut(false);
      if (!next.authenticated) { setState(null); return; }
      try {
        await refreshState();
        setStateError("");
      } catch (err) {
        if (isSignedOut(err)) { setSignedOut(true); setSession({ setup_required: false, authenticated: false }); setState(null); return; }
        setStateError(err instanceof Error ? err.message : "Could not load your stream");
      }
    } catch (err) { setError(err instanceof Error ? err.message : "Unable to reach Relay"); }
  }, [refreshState]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const timer = window.setTimeout(() => {
      setInviteToken(params.get("invite"));
      setInviteChecked(true);
      void loadSession();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadSession]);
  useEffect(() => {
    if (!session?.authenticated) return;
    let cancelled = false;
    let timer = 0;
    let misses = 0;
    // A self-scheduling chain rather than setInterval, so slow responses cannot
    // pile up on top of each other.
    const tick = async () => {
      try {
        await refreshState();
        misses = 0;
        if (!cancelled) setStaleSince(0);
      } catch (err) {
        if (isSignedOut(err)) {
          if (!cancelled) { setSignedOut(true); setSession({ setup_required: false, authenticated: false }); setState(null); }
          return;
        }
        misses += 1;
        if (!cancelled && misses >= 3) setStaleSince(misses);
      }
      if (!cancelled) timer = window.setTimeout(tick, 2000);
    };
    timer = window.setTimeout(tick, 2000);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [session?.authenticated, refreshState]);

  useEffect(() => {
    if (!onboardingUsername) return;
    const storageKey = "relay-onboarding-v1:" + onboardingUsername;
    if (window.localStorage.getItem(storageKey)) return;
    const timer = window.setTimeout(() => setShowOnboardingOffer(true), 700);
    return () => window.clearTimeout(timer);
  }, [onboardingUsername]);

  function rememberOnboarding(status: "dismissed" | "complete") {
    if (onboardingUsername) window.localStorage.setItem("relay-onboarding-v1:" + onboardingUsername, status);
  }

  function dismissOnboardingOffer() {
    rememberOnboarding("dismissed");
    setShowOnboardingOffer(false);
  }

  function finishOnboarding() {
    rememberOnboarding("complete");
    setShowOnboarding(false);
  }
  function clearInvite() {
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    setInviteToken(null);
  }

  if (!inviteChecked || !session) return <div className="center-screen"><div className="loader" /><p>Connecting to Relay…</p>{error && <span className="form-error">{error}</span>}</div>;
  if (inviteToken && session.authenticated) return <InviteAlreadySignedIn onReturn={clearInvite} />;
  if (inviteToken) return <AcceptInvite token={inviteToken} onCancel={clearInvite} onComplete={async () => { clearInvite(); await loadSession(); }} />;
  if (session.setup_required) return <Setup onComplete={loadSession} />;
  if (!session.authenticated) return <Login onComplete={loadSession} notice={signedOut ? "Your session ended. Sign in again to keep control of your stream." : ""} />;
  if (!state) return <div className="center-screen">{stateError ? <><p className="form-error">{stateError}</p><button className="secondary" type="button" onClick={() => { setStateError(""); void loadSession(); }}>Try again</button></> : <><div className="loader" /><p>Loading your stream…</p></>}</div>;

  const isLive = state.stream.media.online;
  const isAvailable = state.stream.media.available;
  // What the publisher is actually sending, so every place that names a track
  // can name the one the forwarders are really mapping. null means "not known
  // yet" and the documented two-track layout is assumed, as in the router.
  const arrivingTracks = audioTrackCount(state.stream.media);
  // pendingToggle is a snapshot taken when the switch was clicked. The start
  // dialog can change music_fallback before starting, so read the row back out
  // of the refreshed state or the pills would keep drawing the old choice.
  const toggleLive = pendingToggle ? state.destinations.find((item) => item.id === pendingToggle.id) ?? pendingToggle : null;
  const programMode = state.screens.program_mode;
  const programLabel = programMode === "starting_soon" ? "Starting Soon" : programMode === "brb" ? "BRB" : "Live input";
  const manualScreen = programMode !== "live";
  // "Armed" was a hard-coded literal; drive it from whether a screen is actually
  // ready to take over.
  const protectionArmed = state.screens.brb.status === "ready";

  async function confirmToggle() {
    if (!pendingToggle) return;
    const destination = pendingToggle;
    setError("");
    setToggleBusy(true);
    try {
      await json(`/api/destinations/${destination.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json", "X-CSRF-Token": state!.csrf },
        body: JSON.stringify({ enabled: !destination.enabled }),
      });
      await refreshState();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not change destination"); }
    finally { setToggleBusy(false); setPendingToggle(null); }
  }

  async function removeDestination() {
    if (!pendingRemove) return;
    setModalError("");
    setToggleBusy(true);
    try {
      await json(`/api/destinations/${pendingRemove.id}`, { method: "DELETE", headers: { "X-CSRF-Token": state!.csrf } });
      await refreshState();
      setPendingRemove(null);
    } catch (err) { setModalError(err instanceof Error ? err.message : "Could not remove this destination"); }
    finally { setToggleBusy(false); }
  }

  async function applyMusicFallback(destination: Destination, enabled: boolean, inDialog = false) {
    if (inDialog) setModalError(""); else setError("");
    setFallbackBusy(destination.id);
    try {
      await json(`/api/destinations/${destination.id}/music-fallback`, {
        method: "PATCH", headers: { "Content-Type": "application/json", "X-CSRF-Token": state!.csrf },
        body: JSON.stringify({ enabled }),
      });
      await refreshState();
      setPendingMute(null);
    } catch (err) {
      // The backdrop hides the page-level notice completely, so a failure raised
      // from inside a dialog has to render there — and the dialog has to stay
      // open, or the operator never sees why their choice did not stick.
      const message = err instanceof Error ? err.message : "Could not change the audio fallback";
      if (inDialog) setModalError(message); else setError(message);
    }
    finally { setFallbackBusy(0); }
  }

  function chooseMusicFallback(destination: Destination, enabled: boolean) {
    if (musicFallbackOn(destination) === enabled) return;
    // Muting is the direction that gets the dialog. It is the departure from
    // what Relay does by default, and the only one of the two that can end with
    // a destination refusing the stream or archiving it silent. Choosing
    // Track 1 puts the destination back on the default, so it applies at once.
    if (!enabled) { setModalError(""); setPendingMute(destination); return; }
    void applyMusicFallback(destination, true);
  }

  async function setFastFailover(enabled: boolean) {
    setError("");
    setProtectionBusy(true);
    try {
      await json("/api/stream/fast-failover", {
        method: "PATCH", headers: { "Content-Type": "application/json", "X-CSRF-Token": state!.csrf },
        body: JSON.stringify({ enabled }),
      });
      await refreshState();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not change failover speed"); }
    finally { setProtectionBusy(false); }
  }

  async function setUltraFailover(enabled: boolean) {
    setError("");
    setProtectionBusy(true);
    try {
      await json("/api/stream/ultra-failover", {
        method: "PATCH", headers: { "Content-Type": "application/json", "X-CSRF-Token": state!.csrf },
        body: JSON.stringify({ enabled }),
      });
      await refreshState();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not change failover speed"); }
    finally { setProtectionBusy(false); }
  }

  async function logout() {
    await json("/api/logout", { method: "POST", headers: { "X-CSRF-Token": state!.csrf } });
    setSession({ setup_required: false, authenticated: false }); setState(null);
  }

  async function copyObs() {
    await navigator.clipboard.writeText(state!.stream.obs_url); setCopied(true); window.setTimeout(() => setCopied(false), 1800);
  }

  async function uploadScreen(kind: "brb" | "starting_soon", file: File | undefined) {
    if (!file) return;
    setError(""); setUploadBusy(kind);
    const form = new FormData(); form.append("file", file);
    try {
      await json(`/api/screens/${kind}`, { method:"POST", headers:{"X-CSRF-Token":state!.csrf}, body:form });
      await refreshState();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not upload screen"); }
    finally { setUploadBusy(""); }
  }

  async function changeScreenMode(mode: "live" | "brb" | "starting_soon") {
    setError(""); setProtectionBusy(true);
    try {
      await json("/api/screens/mode", { method:"PATCH", headers:{"Content-Type":"application/json","X-CSRF-Token":state!.csrf}, body:JSON.stringify({mode}) });
      await refreshState();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not change the program source"); }
    finally { setProtectionBusy(false); setPendingScreenMode(null); }
  }

  async function setFailoverAds(enabled: boolean) {
    setError(""); setProtectionBusy(true);
    try {
      await json("/api/twitch/failover-ads", { method:"PATCH", headers:{"Content-Type":"application/json","X-CSRF-Token":state!.csrf}, body:JSON.stringify({enabled}) });
      await refreshState();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not update automatic ads"); }
    finally { setProtectionBusy(false); setPendingAutoArm(false); }
  }

  async function disconnectTwitch() {
    setError(""); setProtectionBusy(true);
    try {
      await json("/api/twitch/disconnect", { method:"POST", headers:{"X-CSRF-Token":state!.csrf} });
      await refreshState();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not disconnect Twitch"); }
    finally { setProtectionBusy(false); }
  }

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark"><i /><i /><i /></span><span>Relay</span></div>
        <nav aria-label="Main navigation"><a className="nav-item active" href="#overview">Overview</a><a className="nav-item" href="#protection">Protection</a><a className="nav-item" href="#destinations">Destinations</a>{state.user.role === "owner" && <a className="nav-item" href="#team">Team</a>}<button className="nav-item nav-button" onClick={() => setShowObs(true)}>OBS setup</button><button className="nav-item nav-button" onClick={() => { setShowOnboardingOffer(false); setShowOnboarding(true); }}>Quick start</button></nav>
        <div className="sidebar-foot"><div className="avatar">{state.user.display_name[0]?.toUpperCase()}</div><div><strong>{state.user.display_name}</strong><span>{state.user.role === "owner" ? "Owner" : "Streamer"}</span></div><button className="logout" onClick={logout}>Sign out</button></div>
      </aside>

      <section className="content" id="overview">
        <header className="topbar"><div><p className="eyebrow">YOUR STREAM</p><h1>Broadcast control</h1></div><div className={`signal ${isLive && !manualScreen ? "" : "fallback"}`}><span />{manualScreen ? `${programLabel} on air` : isLive ? "Signal connected" : isAvailable ? "Backup screen active" : "Waiting for OBS"}</div></header>
        {error && <div className="notice error-notice">{error}<button onClick={() => setError("")}>Dismiss</button></div>}
        {Boolean(staleSince) && <div className="notice stale-notice" role="status">Reconnecting to Relay — these readings may be out of date.</div>}
        {manualScreen && isLive && <div className="notice error-notice" role="alert">A manual screen is on air but OBS is still publishing. Destinations may be carrying your live camera.</div>}
        <TrackTwoAlert destinations={state.destinations} arriving={arrivingTracks} />

        <section className="hero-grid">
          <article className="preview-card">
            {showMonitor && isAvailable ? <iframe title="Live monitor" src={`/media/${state.stream.slug}?muted=false`} allow="autoplay; fullscreen" /> :
              <div className={`preview ${isLive && !manualScreen ? "" : "offline"}`}><div className="preview-copy"><span className={isLive && !manualScreen ? "live-pill" : "backup-pill"}>{manualScreen ? programLabel.toUpperCase() : isLive ? "LIVE INPUT" : "PROTECTED"}</span><strong>{manualScreen ? `${programLabel} is on air` : isLive ? "OBS connected" : "Backup screen ready"}</strong><small>{manualScreen ? "OBS reconnect is paused by Relay" : state.stream.media.tracks.length ? `${state.stream.media.tracks.length} media tracks detected` : "Waiting for track information"}</small></div><div className="audio-bars" aria-hidden="true">{Array.from({ length: 16 }).map((_, index) => <i key={index} />)}</div></div>}
            <footer><div><span className={`status-dot ${isLive && !manualScreen ? "" : "amber"}`} /><strong>{manualScreen ? `${programLabel} is feeding destinations` : isLive ? "OBS is sending" : "Connection protection is active"}</strong></div><button type="button" onClick={() => setShowMonitor((value) => !value)}>{showMonitor ? "Close monitor" : "Open monitor"}</button></footer>
          </article>
          <article className="protection-card">
            <div className="protection-icon"><span /></div>
            <p className="eyebrow">CONNECTION PROTECTION</p>
            <h2>Ready if your internet drops</h2>
            <p>Your backup screen keeps active destinations online while OBS reconnects.</p>
            <div className="fast-failover">
              <div><strong>Fast handoff</strong><span>{state.stream.fast_failover ? `Switches after ${state.stream.fast_failover_seconds}s of silence` : "Waits for the media server to time out"}</span></div>
              <button className={`toggle ${state.stream.fast_failover ? "on" : ""}`} type="button" role="switch" aria-checked={state.stream.fast_failover} aria-label={state.stream.fast_failover ? "Turn off fast handoff" : "Turn on fast handoff"} disabled={protectionBusy} onClick={() => setFastFailover(!state.stream.fast_failover)}><span /></button>
            </div>
            <div className="fast-failover">
              <div><strong>Ultra-fast handoff</strong><span>{state.stream.ultra_failover ? `Switches after ${state.stream.ultra_failover_seconds}s of silence` : `Backup takes over after only ${state.stream.ultra_failover_seconds}s`}</span></div>
              <button className={`toggle ${state.stream.ultra_failover ? "on" : ""}`} type="button" role="switch" aria-checked={state.stream.ultra_failover} aria-label={state.stream.ultra_failover ? "Turn off ultra-fast handoff" : "Turn on ultra-fast handoff"} disabled={protectionBusy} onClick={() => setUltraFailover(!state.stream.ultra_failover)}><span /></button>
            </div>
            <div className={`protection-state ${protectionArmed ? "" : "unavailable"}`}><span /> {protectionArmed ? "Armed" : "Backup screen unavailable"}</div>
            {state.stream.stalls && (
              /* The stall ledger: recovered delivery gaps too short for fast
                 handoff to act on. How often these land between 0.5s and the
                 handoff threshold is the evidence for whether the threshold
                 can safely drop. */
              <p className="stall-ledger">
                {state.stream.stalls.over_half_s === 0
                  ? `OBS feed: no delivery gaps over 0.5s in the past ${state.stream.stalls.window_days} days`
                  : `OBS feed: rode out ${state.stream.stalls.over_half_s} delivery gap${state.stream.stalls.over_half_s === 1 ? "" : "s"} over 0.5s (${state.stream.stalls.over_1s} over 1s${state.stream.stalls.longest_s != null ? `, longest ${state.stream.stalls.longest_s}s` : ""}) in the past ${state.stream.stalls.window_days} days`}
              </p>
            )}
          </article>
        </section>

        <section className="control-grid" id="protection">
          <article className="control-card">
            <div className="control-card-head"><div><p className="eyebrow">PROGRAM SOURCE</p><h2>Stream screens</h2></div><span className={`status-chip ${programMode === "live" ? "disabled" : "armed"}`}>{programLabel}</span></div>
            <p className="control-copy">Choose what viewers see. Turning on a screen safely takes over from OBS while keeping enabled destinations connected.</p>
            <div className="screen-list">
              <div className="screen-item">
                <div><strong>BRB / connection lost</strong><span>{state.screens.brb.stale ? "Re-upload to get the faster handoff" : state.screens.brb.status === "ready" ? state.screens.brb.original_name || "Default screen ready" : state.screens.brb.status === "converting" ? "Converting on the VPS…" : state.screens.brb.status === "error" ? state.screens.brb.message : "Default screen unavailable"}</span></div>
                <div className="screen-item-controls"><label className={"upload-button " + (uploadBusy === "brb" ? "busy" : "")}>{uploadBusy === "brb" ? "Uploading…" : "Upload"}<input type="file" accept="video/*,.mov,.mkv,.mp4,.webm" disabled={Boolean(uploadBusy)} onChange={(event) => { uploadScreen("brb", event.target.files?.[0]); event.currentTarget.value = ""; }} /></label><button className={`toggle screen-toggle ${programMode === "brb" ? "on" : ""}`} type="button" role="switch" aria-checked={programMode === "brb"} aria-label={programMode === "brb" ? "Turn off BRB and return to live input" : "Put BRB on air"} disabled={protectionBusy || state.screens.brb.status !== "ready"} onClick={() => setPendingScreenMode(programMode === "brb" ? "live" : "brb")}><span /></button></div>
              </div>
              <div className="screen-item">
                <div><strong>Starting Soon</strong><span>{state.screens.starting_soon.stale ? "Re-upload to get the faster handoff" : state.screens.starting_soon.status === "ready" ? state.screens.starting_soon.original_name || "Ready" : state.screens.starting_soon.status === "converting" ? "Converting on the VPS…" : state.screens.starting_soon.status === "error" ? state.screens.starting_soon.message : "Upload a video to enable"}</span></div>
                <div className="screen-item-controls"><label className={"upload-button " + (uploadBusy === "starting_soon" ? "busy" : "")}>{uploadBusy === "starting_soon" ? "Uploading…" : "Upload"}<input type="file" accept="video/*,.mov,.mkv,.mp4,.webm" disabled={Boolean(uploadBusy)} onChange={(event) => { uploadScreen("starting_soon", event.target.files?.[0]); event.currentTarget.value = ""; }} /></label><button className={`toggle screen-toggle ${programMode === "starting_soon" ? "on" : ""}`} type="button" role="switch" aria-checked={programMode === "starting_soon"} aria-label={programMode === "starting_soon" ? "Turn off Starting Soon and return to live input" : "Put Starting Soon on air"} disabled={protectionBusy || state.screens.starting_soon.status !== "ready"} onClick={() => setPendingScreenMode(programMode === "starting_soon" ? "live" : "starting_soon")}><span /></button></div>
              </div>
            </div>
            <div className="screen-actions">{programMode !== "live" && <button className="primary" disabled={protectionBusy} onClick={() => setPendingScreenMode("live")}>Return to live input</button>}<span>{programMode === "live" ? "Both screen switches are off. OBS is the program source." : "Relay will allow OBS to reconnect when you return to live input."}</span></div>
          </article>
          <article className="control-card twitch-control">
            <div className="control-card-head"><div><p className="eyebrow">TWITCH FAILOVER</p><h2>Automatic outage ad</h2></div><span className={"status-chip " + (state.twitch.failover_ads_enabled ? "armed" : "disabled")}>{state.twitch.failover_ads_enabled ? "Armed" : "Disabled"}</span></div>
            <p className="control-copy">After OBS has been gone for a full {state.twitch.grace_seconds} seconds, Relay can run the smallest ad needed to restore 60 minutes of preroll-free time.</p>
            {!state.twitch.available ? <div className="integration-empty">Twitch sign-in is not configured on this server yet.</div> : !state.twitch.connected ? <div className="integration-connect"><div><strong>Connect your broadcaster account</strong><span>Relay requests only ad schedule and commercial permissions.</span></div><a className="primary button-link" href="/api/twitch/connect">Connect Twitch</a></div> : <><div className="integration-account"><div className="twitch-avatar">T</div><div><strong>{state.twitch.login}</strong><span>Twitch broadcaster connected</span></div><button className="text-button" disabled={protectionBusy} onClick={disconnectTwitch}>Disconnect</button></div><div className="auto-ad-actions">{state.twitch.failover_ads_enabled ? <button className="danger-outline" disabled={protectionBusy} onClick={() => setFailoverAds(false)}>Disable automatic ads</button> : <button className="primary" disabled={protectionBusy} onClick={() => setPendingAutoArm(true)}>Arm automatic ads</button>}<span>No approval is requested during an outage.</span></div>{state.twitch.last_event && <div className={"ad-event " + state.twitch.last_event.status}><strong>Last outage check: {state.twitch.last_event.status.replace("_", " ")}</strong><span>{state.twitch.last_event.message}</span></div>}</>}
          </article>
        </section>

        <section className="metrics-grid" id="metrics" aria-label="Stream health">
          <ContributionPanel signal={state.stream.signal} media={state.stream.media} programMode={programMode} stalls={state.stream.stalls} />
          <OutputsPanel destinations={state.destinations} />
        </section>

        <section className="destinations" id="destinations">
          <div className="section-heading"><div><p className="eyebrow">OUTPUTS</p><h2>Destinations</h2></div><button className="secondary" type="button" onClick={() => setShowAdd(true)}>+ Add destination</button></div>
          {state.destinations.length ? <div className="destination-list">{state.destinations.map((destination) => {
            const status = destinationStatus(destination);
            const audio = effectiveAudio(destination, arrivingTracks);
            // The fallback only decides something while OBS is short a track and
            // this destination is actually forwarding. Anywhere else it is a
            // setting with no consequence, so the row shows nothing at all.
            const decidingFallback = hasMusicFallbackChoice(destination.platform) && arrivingTracks === 1 && Boolean(destination.enabled);
            const carryingMusic = musicFallbackOn(destination);
            return (
            <div className="destination-row" key={destination.id}>
              <PlatformIcon platform={destination.platform} fallback={destination.name[0]?.toUpperCase()} /><div className="destination-name"><strong>{destination.name}</strong><span className={`destination-audio ${audio.tone}`}>{audio.label}</span></div>
              <div className={`route-status ${status.tone}`}><span />{status.label}</div>
              <div className="destination-actions">
                <button className="text-button" type="button" aria-label={`Remove ${destination.name}`} onClick={() => { setModalError(""); setPendingRemove(destination); }}>Remove</button>
                <button className={`toggle ${destination.enabled ? "on" : ""}`} type="button" role="switch" aria-checked={Boolean(destination.enabled)} aria-label={`${destination.enabled ? "Stop" : "Start"} ${destination.name} forwarding`} onClick={() => { setModalError(""); setPendingToggle(destination); }}><span /></button>
              </div>
              {destination.last_error && Boolean(destination.enabled) && <p className="destination-error">{destination.last_error}</p>}
              {/* One line, one action. "is set to send" rather than "is sending"
                  for the muted case: Relay picks the mapping when a forwarder
                  starts and this PATCH deliberately does not restart it, so the
                  stored choice and the live `-map` set can disagree. The row's
                  own audio label carries that caveat once, quietly. */}
              {decidingFallback && <p className="fallback-line">
                <span className="fallback-mark" aria-hidden="true">⚠</span>
                <span>Track 2 isn’t arriving, so this is set to send {carryingMusic ? "Track 1, which carries your music." : "no audio at all."}</span>
                <button type="button" className="text-button fallback-action" disabled={fallbackBusy === destination.id}
                  aria-label={carryingMusic ? `Mute ${destination.name} instead while Track 2 is missing` : `Send Track 1 to ${destination.name} while Track 2 is missing`}
                  onClick={() => chooseMusicFallback(destination, !carryingMusic)}>{carryingMusic ? "Mute instead" : "Send Track 1"}</button>
              </p>}
            </div>);
          })}</div> : <div className="empty-state"><strong>No destinations yet</strong><span>Add Twitch first, then YouTube — Relay sends YouTube your clean track on its own.</span><button onClick={() => setShowAdd(true)}>Add your first destination</button></div>}
        </section>

        <AudioRoutingPanel media={state.stream.media} />

        {state.user.role === "owner" && <TeamPanel csrf={state.csrf} />}
        <section className="stats" aria-label="Stream summary">
          <div><span>INPUT</span><strong>{manualScreen ? programLabel.toUpperCase() : isLive ? "ONLINE" : "STANDBY"}</strong></div>
          <div><span>VIDEO</span><strong>{videoSummary(state.stream.signal) || "H.264 copy"}</strong></div>
          <div><span>AUDIO</span><strong>{audioSummary(state.stream.media)}</strong></div>
          <div><span>ACTIVE OUTPUTS</span><strong>{state.destinations.filter((item) => item.state === "forwarding").length}/{state.destinations.filter((item) => item.enabled).length}</strong></div>
        </section>
      </section>

      {showOnboardingOffer && <Modal title="Want a quick Relay tour?" onClose={dismissOnboardingOffer}><div className="onboarding-offer"><div className="onboarding-spark">✦</div><p>Take a short walkthrough of OBS, destinations, and connection protection. You can reopen it anytime from <strong>Quick start</strong>.</p></div><div className="modal-actions"><button type="button" className="secondary" onClick={dismissOnboardingOffer}>Not now</button><button type="button" className="primary" onClick={() => { setShowOnboardingOffer(false); setShowOnboarding(true); }}>Start walkthrough</button></div></Modal>}
      {showOnboarding && <OnboardingTour state={state} onClose={() => { rememberOnboarding("dismissed"); setShowOnboarding(false); }} onFinish={finishOnboarding} />}
      {showObs && <Modal title="Connect OBS" onClose={() => setShowObs(false)}><p className="modal-copy">In OBS, choose <strong>Custom</strong> service, paste this into <strong>Server</strong>, and leave Stream Key blank.</p><label htmlFor="obs-url">Private SRT server address</label><div className="copy-field"><input id="obs-url" readOnly value={state.stream.obs_url} /><button onClick={copyObs}>{copied ? "Copied" : "Copy"}</button></div><p className="hint">Then enable audio <strong>Tracks 1 and 2</strong> under Advanced Output. Track 1 is your full live mix with music; Track 2 is the same mix without it.</p></Modal>}
      {showAdd && <AddDestination csrf={state.csrf} twitchIngest={state.twitch_ingest} onClose={() => setShowAdd(false)} onAdded={async () => { setShowAdd(false); await refreshState(); }} />}
      {pendingToggle && toggleLive && <Modal title={`${pendingToggle.enabled ? "Stop" : "Start"} ${pendingToggle.name}?`} onClose={() => { if (!toggleBusy) setPendingToggle(null); }}>
        <p className="modal-copy">{pendingToggle.enabled ? `Relay will disconnect from ${pendingToggle.name}. Viewers there will lose the feed until you turn it on again.` : `Relay will immediately send your live input—or the protected backup screen—to ${pendingToggle.name}.`}</p>
        <div className="confirm-destination"><PlatformIcon platform={pendingToggle.platform} fallback={pendingToggle.name[0]?.toUpperCase()} /><div><strong>{pendingToggle.name}</strong><span>{effectiveAudio(toggleLive, arrivingTracks).label}</span></div></div>
        {/* The one moment the operator is literally going live with a missing
            track, so this is the one place the choice is worth interrupting for.
            It never blocks the start — the pills save on their own and the
            forwarder picks up whichever one is stored when it starts. */}
        {!pendingToggle.enabled && arrivingTracks === 1 && hasMusicFallbackChoice(pendingToggle.platform) && <div className="start-audio">
          <p className="start-audio-warn" role="status"><span className="fallback-mark" aria-hidden="true">⚠</span> <b>OBS is only sending Track 1.</b> There is no clean mix for {pendingToggle.name} right now — pick what it should send until Track 2 comes back.</p>
          <MusicFallbackChoice value={musicFallbackOn(toggleLive)} busy={fallbackBusy === toggleLive.id} group={`start-music-fallback-${toggleLive.id}`} owner={pendingToggle.name} onChoose={(enabled) => applyMusicFallback(toggleLive, enabled, true)} />
          {modalError && <p className="form-error" role="alert">{modalError}</p>}
          <p className="start-audio-note">Track 1 keeps it on the air with your music in it. {archiveConsequence(pendingToggle.platform)} No audio sends video with no audio track at all, which not every ingest is known to accept.</p>
        </div>}
        <div className="modal-actions"><button type="button" className="secondary" disabled={toggleBusy} onClick={() => setPendingToggle(null)}>Cancel</button><button type="button" className={pendingToggle.enabled ? "danger" : "primary"} disabled={toggleBusy || Boolean(fallbackBusy)} onClick={confirmToggle}>{toggleBusy ? "Working…" : pendingToggle.enabled ? "Stop forwarding" : "Start forwarding"}</button></div>
      </Modal>}
      {pendingScreenMode && <Modal title={pendingScreenMode === "live" ? "Return to live input?" : pendingScreenMode === "brb" ? "Put BRB on air?" : "Put Starting Soon on air?"} onClose={() => { if (!protectionBusy) setPendingScreenMode(null); }}><p className="modal-copy">{pendingScreenMode === "live" ? "Relay will allow OBS to reconnect. The BRB screen stays on air until the OBS signal arrives." : programMode === "live" ? "Relay will temporarily disconnect and hold OBS so this screen can feed every enabled destination. Automatic outage ads will not run during a manual screen." : "Relay will switch the current program screen while keeping enabled destinations connected."}</p><div className="confirm-destination screen-confirm"><div className="screen-confirm-icon">{pendingScreenMode === "live" ? "●" : pendingScreenMode === "brb" ? "B" : "S"}</div><div><strong>{pendingScreenMode === "live" ? "Live OBS input" : pendingScreenMode === "brb" ? "BRB / connection lost" : "Starting Soon"}</strong><span>{pendingScreenMode === "live" ? "OBS reconnect permitted" : "Manual program takeover"}</span></div></div><div className="modal-actions"><button type="button" className="secondary" disabled={protectionBusy} onClick={() => setPendingScreenMode(null)}>Cancel</button><button type="button" className="primary" disabled={protectionBusy} onClick={() => changeScreenMode(pendingScreenMode)}>{protectionBusy ? "Switching…" : pendingScreenMode === "live" ? "Return to live input" : "Put screen on air"}</button></div></Modal>}
      {pendingRemove && <Modal title={`Remove ${pendingRemove.name}?`} onClose={() => { if (!toggleBusy) setPendingRemove(null); }}>
        <p className="modal-copy">Relay stops forwarding to {pendingRemove.name} and forgets its stream key. You can add it again with a new key at any time.</p>
        <div className="confirm-destination"><PlatformIcon platform={pendingRemove.platform} fallback={pendingRemove.name[0]?.toUpperCase()} /><div><strong>{pendingRemove.name}</strong><span>{effectiveAudio(pendingRemove, arrivingTracks).label}</span></div></div>
        {modalError && <p className="form-error" role="alert">{modalError}</p>}
        <div className="modal-actions"><button type="button" className="secondary" disabled={toggleBusy} onClick={() => setPendingRemove(null)}>Cancel</button><button type="button" className="danger" disabled={toggleBusy} onClick={removeDestination}>{toggleBusy ? "Removing…" : "Remove destination"}</button></div>
      </Modal>}
      {pendingMute && <Modal title={`Mute ${pendingMute.name} when Track 2 is missing?`} onClose={() => { if (!fallbackBusy) setPendingMute(null); }}>
        <p className="modal-copy">Only while Track 2 is missing. As soon as OBS sends both tracks again, {pendingMute.name} goes back to the clean mix.</p>
        <p className="modal-copy">Relay will send video with no audio track at all instead of your music mix. {platformMarks[pendingMute.platform]?.label || pendingMute.platform} may refuse a stream shaped that way, or keep a silent recording — pick this only if that is better than music in the archive.</p>
        <div className="confirm-destination fallback-confirm"><PlatformIcon platform={pendingMute.platform} fallback={pendingMute.name[0]?.toUpperCase()} /><div><strong>{pendingMute.name}</strong><span>Fallback becomes silence — no audio track</span></div></div>
        {Boolean(pendingMute.enabled) && <p className="hint">Relay picks the mapping when a forwarder starts, so this applies the next time {pendingMute.name} starts. Nothing already going out is interrupted.</p>}
        {modalError && <p className="form-error" role="alert">{modalError}</p>}
        <div className="modal-actions"><button type="button" className="secondary" disabled={Boolean(fallbackBusy)} onClick={() => setPendingMute(null)}>Cancel</button><button type="button" className="danger" disabled={Boolean(fallbackBusy)} onClick={() => applyMusicFallback(pendingMute, false, true)}>{fallbackBusy ? "Saving…" : "Mute audio"}</button></div>
      </Modal>}
      {pendingAutoArm && <Modal title="Arm automatic outage ads?" onClose={() => { if (!protectionBusy) setPendingAutoArm(false); }}><p className="modal-copy">If a real OBS connection drops and stays offline for one minute, Relay will check Twitch and run the minimum commercial needed to restore 60 minutes of preroll-free time. It will make one attempt and will not ask for approval while you are offline.</p><div className="confirm-destination auto-ad-confirm"><PlatformIcon platform="twitch" /><div><strong>Automatic Twitch outage ad</strong><span>Persistent until you disable it</span></div></div><div className="modal-actions"><button className="secondary" disabled={protectionBusy} onClick={() => setPendingAutoArm(false)}>Cancel</button><button className="primary" disabled={protectionBusy} onClick={() => setFailoverAds(true)}>{protectionBusy ? "Arming…" : "Arm automatic ads"}</button></div></Modal>}
    </main>
  );
}

function OnboardingTour({ state, onClose, onFinish }: { state:StreamState; onClose:()=>void; onFinish:()=>void }) {
  const [step, setStep] = useState(0);
  const [copied, setCopied] = useState(false);

  async function copyAddress() {
    await navigator.clipboard.writeText(state.stream.obs_url);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  }

  return <Modal title="Relay quick start" onClose={onClose}>
    <div className="tour-progress"><span>STEP {step + 1} OF 3</span><div>{[0, 1, 2].map((item) => <i className={item === step ? "active" : item < step ? "done" : ""} key={item} />)}</div></div>
    {step === 0 && <div className="tour-panel">
      <p className="eyebrow">CONNECT YOUR ENCODER</p>
      <h3>Send OBS to your private relay</h3>
      <ol className="tour-list"><li>Open <strong>Settings → Stream</strong> in OBS.</li><li>Choose <strong>Custom</strong> as the service.</li><li>Paste the address below into <strong>Server</strong> and leave <strong>Stream Key</strong> blank.</li><li>Under Advanced Output, enable streaming audio <strong>Tracks 1 and 2</strong> — Track 1 your full mix, Track 2 the same mix with the music muted.</li></ol>
      <label className="tour-label" htmlFor="tour-obs-url">Your private SRT address</label><div className="copy-field"><input id="tour-obs-url" readOnly value={state.stream.obs_url} /><button type="button" onClick={copyAddress}>{copied ? "Copied" : "Copy"}</button></div>
      <div className={"tour-signal " + (state.stream.media.online ? "online" : "")}><span />{state.stream.media.online ? "OBS signal detected" : "Waiting for OBS — you can continue for now"}</div>
    </div>}
    {step === 1 && <div className="tour-panel">
      <p className="eyebrow">CHOOSE YOUR OUTPUTS</p>
      <h3>Add destinations with stream keys</h3>
      <p className="tour-copy">Open <strong>Destinations</strong>, select a service, and paste only its stream key. Relay fills in the ingest server and asks for confirmation before forwarding.</p>
      <div className="tour-platforms"><div><PlatformIcon platform="twitch" /><strong>Twitch</strong><small>Live mix plus clean VOD</small></div><div><PlatformIcon platform="youtube" /><strong>YouTube</strong><small>Primary and backup ingest</small></div><div><PlatformIcon platform="rplay" /><strong>RPLAY</strong><small>Key-only setup</small></div><div><PlatformIcon platform="x" /><strong>X</strong><small>Key-only setup</small></div></div>
      <p className="tour-note">Destinations remain off until you deliberately turn each one on.</p>
    </div>}
    {step === 2 && <div className="tour-panel">
      <p className="eyebrow">STAY ON AIR</p>
      <h3>Let Relay protect the broadcast</h3>
      <div className="tour-feature"><span>1</span><div><strong>BRB fallback</strong><p>If OBS drops, Relay keeps enabled destinations alive with your BRB video.</p></div></div>
      <div className="tour-feature"><span>2</span><div><strong>Manual screens</strong><p>Use Starting Soon or BRB as the program source, then choose Return to live input.</p></div></div>
      <div className="tour-feature"><span>3</span><div><strong>Optional Twitch protection</strong><p>Connect Twitch and arm automatic outage ads only when you want that behavior.</p></div></div>
      <p className="tour-note">You can reopen this walkthrough anytime from <strong>Quick start</strong> in the sidebar.</p>
    </div>}
    <div className="tour-actions"><button type="button" className="secondary" disabled={step === 0} onClick={() => setStep((value) => value - 1)}>Back</button>{step < 2 ? <button type="button" className="primary" onClick={() => setStep((value) => value + 1)}>Next</button> : <button type="button" className="primary" onClick={onFinish}>Finish</button>}</div>
  </Modal>;
}
function Setup({ onComplete }: { onComplete: () => Promise<void> }) {
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); setError(""); const data = new FormData(event.currentTarget); try { await json("/api/setup", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(Object.fromEntries(data)) }); await onComplete(); } catch (err) { setError(err instanceof Error ? err.message : "Setup failed"); } }
  return <AuthShell eyebrow="FIRST-RUN SETUP" title="Create the owner account" copy="This account controls team invitations and every stream on this relay."><form onSubmit={submit}><label>One-time setup code<input name="token" required autoComplete="off" /></label><label>Display name<input name="display_name" required defaultValue="twish" /></label><label>Username<input name="username" required defaultValue="twish" autoCapitalize="none" /></label><label>Password<input name="password" type="password" required minLength={12} autoComplete="new-password" /></label>{error && <span className="form-error">{error}</span>}<button className="primary" type="submit">Create owner account</button></form></AuthShell>;
}

function Login({ onComplete, notice = "" }: { onComplete: () => Promise<void>; notice?: string }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); setError(""); setBusy(true); const data = new FormData(event.currentTarget); try { await json("/api/login", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(Object.fromEntries(data)) }); await onComplete(); } catch (err) { setError(err instanceof Error ? err.message : "Sign in failed"); setBusy(false); } }
  return <AuthShell eyebrow="WELCOME BACK" title="Sign in to Relay" copy="Your streams and destinations are waiting.">{notice && <p className="auth-notice" role="status">{notice}</p>}<form onSubmit={submit}><label>Username<input name="username" required autoCapitalize="none" /></label><label>Password<input name="password" type="password" required autoComplete="current-password" /></label>{error && <span className="form-error">{error}</span>}<button className="primary" type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button></form></AuthShell>;
}

function InviteAlreadySignedIn({ onReturn }: { onReturn:()=>void }) {
  return <AuthShell eyebrow="TEAM INVITATION" title="You’re already signed in" copy="Invite links create a new streamer account. Send this link to your teammate, or open it in a private browser window."><button className="primary" type="button" onClick={onReturn}>Return to your dashboard</button></AuthShell>;
}

function AcceptInvite({ token, onCancel, onComplete }: { token:string; onCancel:()=>void; onComplete:()=>Promise<void> }) {
  const [info, setInfo] = useState<InviteInfo | null>(null);
  const [lookupError, setLookupError] = useState("");
  const [submitError, setSubmitError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    json<InviteInfo>("/api/invite", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({token}) })
      .then((result) => { if (!cancelled) setInfo(result); })
      .catch((err) => { if (!cancelled) setLookupError(err instanceof Error ? err.message : "This invitation is unavailable"); });
    return () => { cancelled = true; };
  }, [token]);

  if (lookupError) return <AuthShell eyebrow="TEAM INVITATION" title="This invite is unavailable" copy={lookupError}><button className="secondary" type="button" onClick={onCancel}>Go to sign in</button></AuthShell>;
  if (!info) return <div className="center-screen"><div className="loader" /><p>Checking your invitation…</p></div>;

  const suggested = info.label.toLowerCase().normalize("NFKD").replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitError("");
    setBusy(true);
    const data = Object.fromEntries(new FormData(event.currentTarget));
    try {
      await json("/api/invite/accept", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({...data, token}) });
      await onComplete();
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Could not create your account");
      setBusy(false);
    }
  }

  return <AuthShell eyebrow="YOU’RE INVITED" title="Join the stream team" copy={`${info.invited_by} invited you to your own private Relay workspace. Your destinations, Twitch login, backup screens, and OBS address stay separate.`}><div className="invite-context"><span>INVITE FOR</span><strong>{info.label}</strong><small>Expires {formatDate(info.expires_at)}</small></div><form onSubmit={submit}><label>Display name<input name="display_name" required defaultValue={info.label} autoComplete="name" /></label><label>Username<input name="username" required minLength={3} maxLength={32} pattern="[a-z0-9][a-z0-9_-]{2,31}" defaultValue={suggested.length >= 3 ? suggested : ""} autoCapitalize="none" autoComplete="username" /><span className="field-hint">Lowercase letters, numbers, dashes, or underscores.</span></label><label>Password<input name="password" type="password" required minLength={12} autoComplete="new-password" /><span className="field-hint">At least 12 characters.</span></label>{submitError && <span className="form-error">{submitError}</span>}<button className="primary" type="submit" disabled={busy}>{busy ? "Creating your workspace…" : "Create my account"}</button></form></AuthShell>;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { month:"short", day:"numeric", hour:"numeric", minute:"2-digit" }).format(new Date(value));
}

function TeamPanel({ csrf }: { csrf:string }) {
  const [team, setTeam] = useState<TeamState | null>(null);
  const [error, setError] = useState("");
  const [showInvite, setShowInvite] = useState(false);
  const [inviteResult, setInviteResult] = useState<InviteResult | null>(null);
  const [inviteBusy, setInviteBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [pendingMember, setPendingMember] = useState<TeamMember | null>(null);
  const [pendingInvite, setPendingInvite] = useState<TeamInvite | null>(null);
  const [actionBusy, setActionBusy] = useState(false);

  const loadTeam = useCallback(async () => {
    try { setTeam(await json<TeamState>("/api/team")); setError(""); }
    catch (err) { setError(err instanceof Error ? err.message : "Could not load your team"); }
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => { void loadTeam(); }, 0);
    return () => window.clearTimeout(timer);
  }, [loadTeam]);

  async function createInvite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setInviteBusy(true);
    setError("");
    const data = Object.fromEntries(new FormData(event.currentTarget));
    try {
      const result = await json<InviteResult>("/api/team/invites", { method:"POST", headers:{"Content-Type":"application/json","X-CSRF-Token":csrf}, body:JSON.stringify({ label:data.label, expires_in_days:Number(data.expires_in_days) }) });
      setInviteResult(result);
      await loadTeam();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not create invitation"); }
    finally { setInviteBusy(false); }
  }

  async function copyInvite() {
    if (!inviteResult) return;
    await navigator.clipboard.writeText(inviteResult.invite_url);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  }

  async function updateMember() {
    if (!pendingMember) return;
    setActionBusy(true);
    setError("");
    try {
      await json(`/api/team/users/${pendingMember.id}`, { method:"PATCH", headers:{"Content-Type":"application/json","X-CSRF-Token":csrf}, body:JSON.stringify({enabled:!pendingMember.enabled}) });
      setPendingMember(null);
      await loadTeam();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not update this account"); }
    finally { setActionBusy(false); }
  }

  async function revokeInvite() {
    if (!pendingInvite) return;
    setActionBusy(true);
    setError("");
    try {
      await json(`/api/team/invites/${pendingInvite.id}`, { method:"DELETE", headers:{"X-CSRF-Token":csrf} });
      setPendingInvite(null);
      await loadTeam();
    } catch (err) { setError(err instanceof Error ? err.message : "Could not revoke this invitation"); }
    finally { setActionBusy(false); }
  }

  return <section className="team-section" id="team">
    <div className="section-heading"><div><p className="eyebrow">ACCESS</p><h2>Stream team</h2></div><button className="secondary" type="button" onClick={() => { setInviteResult(null); setCopied(false); setShowInvite(true); }}>+ Invite teammate</button></div>
    <p className="team-copy">Each person gets an isolated stream, OBS address, destinations, Twitch connection, and standby media.</p>
    {error && <div className="notice error-notice">{error}<button onClick={() => setError("")}>Dismiss</button></div>}
    {!team ? <div className="team-loading"><div className="loader" /><span>Loading team…</span></div> : <>
      <div className="member-list">{team.members.map((member) => <div className={`member-row ${member.enabled ? "" : "suspended"}`} key={member.id}>
        <div className="member-avatar">{member.display_name[0]?.toUpperCase()}</div>
        <div className="member-identity"><strong>{member.display_name}</strong><span>@{member.username} · {member.role === "owner" ? "Owner" : "Streamer"}</span></div>
        <div className="member-meta"><span>{member.online ? `Publishing · ${formatNumber((member.receive_mbps ?? 0) * 1000, 0, " kb/s")}` : "Not publishing"}</span><span>{member.forwarding_count ?? 0}/{member.enabled_destination_count} outputs up</span></div>
        <div className={`member-status ${member.enabled ? "active" : ""}`}><span />{member.enabled ? "Active" : "Suspended"}</div>
        {member.role === "owner" ? <span className="owner-lock">Protected</span> : <button className={member.enabled ? "danger-outline member-action" : "primary member-action"} type="button" onClick={() => setPendingMember(member)}>{member.enabled ? "Suspend" : "Restore"}</button>}
      </div>)}</div>
      {team.invitations.length > 0 && <div className="pending-invites"><div className="pending-heading"><strong>Pending invitations</strong><span>Links work once, then disappear.</span></div>{team.invitations.map((invite) => <div className="invite-row" key={invite.id}><div><strong>{invite.label}</strong><span>Expires {formatDate(invite.expires_at)}</span></div><button className="text-button" type="button" onClick={() => setPendingInvite(invite)}>Revoke</button></div>)}</div>}
    </>}
    {showInvite && <Modal title={inviteResult ? "Invitation ready" : "Invite a teammate"} onClose={() => { if (!inviteBusy) { setShowInvite(false); setInviteResult(null); } }}>{inviteResult ? <><p className="modal-copy">Send this private one-time link to your teammate. For safety, Relay cannot show it again after you close this window.</p><label htmlFor="invite-url">Invitation link</label><div className="copy-field"><input id="invite-url" readOnly value={inviteResult.invite_url} /><button type="button" onClick={copyInvite}>{copied ? "Copied" : "Copy"}</button></div><p className="hint">Expires {formatDate(inviteResult.expires_at)}. Your teammate chooses their own username and password.</p><div className="modal-actions"><button className="primary" type="button" onClick={() => { setShowInvite(false); setInviteResult(null); }}>Done</button></div></> : <form onSubmit={createInvite}><p className="modal-copy">Relay will make a private workspace and OBS address when this invitation is accepted.</p><label>Teammate name<input name="label" required maxLength={60} placeholder="Streamer display name" autoComplete="off" /></label><label>Link expires<select name="expires_in_days" defaultValue="7"><option value="1">In 24 hours</option><option value="7">In 7 days</option><option value="30">In 30 days</option></select></label><div className="modal-actions"><button className="secondary" type="button" disabled={inviteBusy} onClick={() => setShowInvite(false)}>Cancel</button><button className="primary" type="submit" disabled={inviteBusy}>{inviteBusy ? "Creating…" : "Create invitation"}</button></div></form>}</Modal>}
    {pendingMember && <Modal title={`${pendingMember.enabled ? "Suspend" : "Restore"} ${pendingMember.display_name}?`} onClose={() => { if (!actionBusy) setPendingMember(null); }}><p className="modal-copy">{pendingMember.enabled ? "Relay will immediately stop their destinations, reject their OBS connection, and disable automatic outage ads. Their settings and uploaded media are preserved." : "They will be able to sign in and use OBS again. Destinations stay off until they choose to restart them."}</p><div className="confirm-member"><div className="member-avatar">{pendingMember.display_name[0]?.toUpperCase()}</div><div><strong>{pendingMember.display_name}</strong><span>@{pendingMember.username}</span></div></div><div className="modal-actions"><button className="secondary" disabled={actionBusy} onClick={() => setPendingMember(null)}>Cancel</button><button className={pendingMember.enabled ? "danger" : "primary"} disabled={actionBusy} onClick={updateMember}>{actionBusy ? "Working…" : pendingMember.enabled ? "Suspend account" : "Restore account"}</button></div></Modal>}
    {pendingInvite && <Modal title={`Revoke ${pendingInvite.label}’s invite?`} onClose={() => { if (!actionBusy) setPendingInvite(null); }}><p className="modal-copy">That link will stop working immediately. You can create a new invitation later.</p><div className="modal-actions"><button className="secondary" disabled={actionBusy} onClick={() => setPendingInvite(null)}>Cancel</button><button className="danger" disabled={actionBusy} onClick={revokeInvite}>{actionBusy ? "Revoking…" : "Revoke invitation"}</button></div></Modal>}
  </section>;
}

function AuthShell({ eyebrow, title, copy, children }: { eyebrow:string; title:string; copy:string; children:React.ReactNode }) { return <main className="auth-page"><section className="auth-brand"><div className="brand big"><span className="brand-mark"><i /><i /><i /></span><span>Relay</span></div><div><p className="eyebrow">BUILT FOR THE STREAM TEAM</p><h2>One clean connection.<br />Everywhere you go live.</h2></div></section><section className="auth-card"><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p>{copy}</p>{children}</section></main>; }

function Modal({ title, onClose, children }: { title:string; onClose:()=>void; children:React.ReactNode }) {
  const container = useRef<HTMLElement>(null);
  // onClose is an inline arrow at every call site, so a new identity arrives on
  // each poll-driven re-render. Holding it in a ref keeps the effect mounted
  // once instead of tearing down and stealing focus twice a second.
  const close = useRef(onClose);
  useEffect(() => { close.current = onClose; }, [onClose]);
  useEffect(() => {
    const root = container.current;
    if (!root) return;
    const previous = document.activeElement as HTMLElement | null;
    root.focus();
    if (!root.contains(document.activeElement)) {
      // Focusing a tabindex="-1" container is not honoured in every state; fall
      // back to the first real control, which is the header's close button.
      root.querySelector<HTMLElement>('button,a[href],input,select,textarea')?.focus();
    }
    // Without a trap, Tab reaches the destination toggles behind the dialog and
    // silently retargets the confirmation that is already open.
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); close.current(); return; }
      if (event.key !== "Tab" || !root!.contains(document.activeElement)) return;
      const focusable = [...root!.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", onKeyDown, true);
    return () => { document.removeEventListener("keydown", onKeyDown, true); previous?.focus?.(); };
  }, []);
  // Deliberately no backdrop-click-to-dismiss: onClose on the invite dialog
  // destroys a one-time link that cannot be shown again.
  return <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={container}><header><h2>{title}</h2><button aria-label="Close" onClick={onClose}>×</button></header>{children}</section></div>;
}

function AddDestination({ csrf, twitchIngest, onClose, onAdded }: { csrf:string; twitchIngest:StreamState["twitch_ingest"]; onClose:()=>void; onAdded:()=>Promise<void> }) {
  const [platform, setPlatform] = useState("twitch");
  const [name, setName] = useState("Twitch");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function choosePlatform(value: string) {
    const names: Record<string, string> = { twitch:"Twitch", youtube:"YouTube", rplay:"RPLAY", x:"X", custom:"Custom" };
    setPlatform(value);
    setName(names[value] || "Custom");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    const data = new FormData(event.currentTarget);
    // music_fallback is deliberately never sent. Adding a destination is not a
    // moment when the setting decides anything — it only matters during a
    // one-track publish — so the router resolves its own per-platform default
    // and the operator meets the choice when it actually costs them something.
    const body: Record<string, unknown> = { name, platform, output_url:String(data.get("output_url") ?? "") };
    try {
      await json("/api/destinations", {
        method:"POST",
        headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},
        body:JSON.stringify(body),
      });
      await onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save destination");
      setBusy(false);
    }
  }

  const keyOnly = platform === "twitch" || platform === "youtube" || platform === "rplay" || platform === "x";
  const platformName = platform === "rplay" ? "RPLAY" : platform === "youtube" ? "YouTube" : platform === "x" ? "X" : "Twitch";
  const route = audioRouteFor(platform);

  return <Modal title="Add destination" onClose={onClose}><form onSubmit={submit}>
    <label>Platform<select name="platform" value={platform} onChange={(event) => choosePlatform(event.target.value)}><option value="twitch">Twitch</option><option value="youtube">YouTube</option><option value="rplay">RPLAY</option><option value="x">X</option><option value="custom">Custom RTMP</option></select></label>
    <label>Display name<input name="name" required value={name} onChange={(event) => setName(event.target.value)} /></label>
    <label>{keyOnly ? platformName + " stream key" : "Full RTMP address with stream key"}<input name="output_url" required type="password" autoComplete="off" placeholder={platform === "twitch" ? "live_…" : keyOnly ? "Paste " + platformName + " key" : "rtmps://…"} /></label>
    {platform === "twitch" && <p className="hint">Relay selects the ingest automatically. Current best: <strong>{twitchIngest.name}</strong>{twitchIngest.latency_ms != null ? " · " + Math.round(twitchIngest.latency_ms) + " ms" : ""}.</p>}
    {platform === "youtube" && <p className="hint">Relay sends matching copies to YouTube’s primary and backup ingests automatically.</p>}
    {platform === "rplay" && <p className="hint">Relay automatically sends this key through <strong>livestream-push.rplay.live</strong>.</p>}
    {platform === "x" && <p className="hint">Relay sends this key through <strong>ca.pscp.tv:80/x</strong>. Paste only the X stream key.</p>}
    <div className="audio-preset"><span>AUDIO</span><strong>{route.maps} · {route.note}</strong><small>{route.why}</small></div>
    {error && <span className="form-error">{error}</span>}
    <div className="modal-actions"><button type="button" className="secondary" disabled={busy} onClick={onClose}>Cancel</button><button className="primary" type="submit" disabled={busy}>{busy ? "Saving…" : "Save destination"}</button></div>
  </form></Modal>;
}
