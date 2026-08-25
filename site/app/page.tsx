"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

type Session = { setup_required: boolean; authenticated: boolean; csrf?: string | null; user?: User | null };
type User = { username: string; display_name: string; role: string };
type Destination = { id: number; name: string; platform: string; audio_track: number; enabled: number; state: string; last_error?: string | null };
type TwitchEvent = { checked_at: string; preroll_before?: number | null; requested_length?: number | null; status: string; message?: string | null };
type TwitchState = { available: boolean; connected: boolean; login?: string | null; failover_ads_enabled: boolean; grace_seconds: number; last_event?: TwitchEvent | null };
type ScreenAsset = { kind: string; status: "missing" | "converting" | "ready" | "error"; original_name?: string | null; message?: string | null; updated_at?: string | null };
type StreamState = {
  user: User;
  csrf: string;
  stream: { slug: string; obs_url: string; media: { available: boolean; online: boolean; tracks: string[]; bytes_received?: number } };
  twitch_ingest: { name: string; latency_ms?: number | null; checked_at?: string | null };
  twitch: TwitchState;
  screens: { mode: "brb" | "starting_soon"; program_mode: "live" | "brb" | "starting_soon"; brb: ScreenAsset; starting_soon: ScreenAsset };
  destinations: Destination[];
};
type TeamMember = { id:number; username:string; display_name:string; role:string; enabled:boolean; created_at:string; slug:string; destination_count:number; enabled_destination_count:number; twitch_connected:boolean };
type TeamInvite = { id:number; label:string; created_at:string; expires_at:string };
type TeamState = { members:TeamMember[]; invitations:TeamInvite[] };
type InviteInfo = { label:string; invited_by:string; expires_at:string };
type InviteResult = { id:number; invite_url:string; expires_at:string };

function audioModeLabel(mode: number): string {
  if (mode === 3) return "Live: Tracks 1 + 2 · VOD: Track 2";
  if (mode === 4) return "Combined mix · Tracks 1 + 2";
  if (mode === 2) return "Track 2 only · clean/game";
  return "Track 1 only · music";
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

async function json<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: "same-origin", ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "Something went wrong");
  return body as T;
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
  const [toggleBusy, setToggleBusy] = useState(false);
  const [pendingAutoArm, setPendingAutoArm] = useState(false);
  const [pendingScreenMode, setPendingScreenMode] = useState<"live" | "brb" | "starting_soon" | null>(null);
  const [protectionBusy, setProtectionBusy] = useState(false);
  const [uploadBusy, setUploadBusy] = useState<"" | "brb" | "starting_soon">("");
  const [inviteToken, setInviteToken] = useState<string | null>(null);
  const [inviteChecked, setInviteChecked] = useState(false);
  const onboardingUsername = state?.user.username;

  const loadSession = useCallback(async () => {
    try {
      const next = await json<Session>("/api/session");
      setSession(next);
      if (next.authenticated) setState(await json<StreamState>("/api/state"));
      else setState(null);
    } catch (err) { setError(err instanceof Error ? err.message : "Unable to reach Relay"); }
  }, []);

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
    const timer = window.setInterval(async () => {
      try { setState(await json<StreamState>("/api/state")); } catch { /* retain last known state */ }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [session?.authenticated]);

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
  if (!session.authenticated) return <Login onComplete={loadSession} />;
  if (!state) return <div className="center-screen"><div className="loader" /><p>Loading your stream…</p></div>;

  const isLive = state.stream.media.online;
  const isAvailable = state.stream.media.available;
  const programMode = state.screens.program_mode;
  const programLabel = programMode === "starting_soon" ? "Starting Soon" : programMode === "brb" ? "BRB" : "Live input";
  const manualScreen = programMode !== "live";

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
      setState(await json<StreamState>("/api/state"));
    } catch (err) { setError(err instanceof Error ? err.message : "Could not change destination"); }
    finally { setToggleBusy(false); setPendingToggle(null); }
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
      setState(await json<StreamState>("/api/state"));
    } catch (err) { setError(err instanceof Error ? err.message : "Could not upload screen"); }
    finally { setUploadBusy(""); }
  }

  async function changeScreenMode(mode: "live" | "brb" | "starting_soon") {
    setError(""); setProtectionBusy(true);
    try {
      await json("/api/screens/mode", { method:"PATCH", headers:{"Content-Type":"application/json","X-CSRF-Token":state!.csrf}, body:JSON.stringify({mode}) });
      setState(await json<StreamState>("/api/state"));
    } catch (err) { setError(err instanceof Error ? err.message : "Could not change the program source"); }
    finally { setProtectionBusy(false); setPendingScreenMode(null); }
  }

  async function setFailoverAds(enabled: boolean) {
    setError(""); setProtectionBusy(true);
    try {
      await json("/api/twitch/failover-ads", { method:"PATCH", headers:{"Content-Type":"application/json","X-CSRF-Token":state!.csrf}, body:JSON.stringify({enabled}) });
      setState(await json<StreamState>("/api/state"));
    } catch (err) { setError(err instanceof Error ? err.message : "Could not update automatic ads"); }
    finally { setProtectionBusy(false); setPendingAutoArm(false); }
  }

  async function disconnectTwitch() {
    setError(""); setProtectionBusy(true);
    try {
      await json("/api/twitch/disconnect", { method:"POST", headers:{"X-CSRF-Token":state!.csrf} });
      setState(await json<StreamState>("/api/state"));
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

        <section className="hero-grid">
          <article className="preview-card">
            {showMonitor && isAvailable ? <iframe title="Live monitor" src={`/media/${state.stream.slug}?muted=false`} allow="autoplay; fullscreen" /> :
              <div className={`preview ${isLive && !manualScreen ? "" : "offline"}`}><div className="preview-copy"><span className={isLive && !manualScreen ? "live-pill" : "backup-pill"}>{manualScreen ? programLabel.toUpperCase() : isLive ? "LIVE INPUT" : "PROTECTED"}</span><strong>{manualScreen ? `${programLabel} is on air` : isLive ? "OBS connected" : "Backup screen ready"}</strong><small>{manualScreen ? "OBS reconnect is paused by Relay" : `${state.stream.media.tracks.length || 3} media tracks detected`}</small></div><div className="audio-bars" aria-hidden="true">{Array.from({ length: 16 }).map((_, index) => <i key={index} />)}</div></div>}
            <footer><div><span className={`status-dot ${isLive && !manualScreen ? "" : "amber"}`} /><strong>{manualScreen ? `${programLabel} is feeding destinations` : isLive ? "OBS is sending" : "Connection protection is active"}</strong></div><button type="button" onClick={() => setShowMonitor((value) => !value)}>{showMonitor ? "Close monitor" : "Open monitor"}</button></footer>
          </article>
          <article className="protection-card"><div className="protection-icon"><span /></div><p className="eyebrow">CONNECTION PROTECTION</p><h2>Ready if your internet drops</h2><p>Your backup screen keeps active destinations online while OBS reconnects.</p><div className="protection-state"><span /> Armed</div></article>
        </section>

        <section className="control-grid" id="protection">
          <article className="control-card">
            <div className="control-card-head"><div><p className="eyebrow">PROGRAM SOURCE</p><h2>Stream screens</h2></div><span className={`status-chip ${programMode === "live" ? "disabled" : "armed"}`}>{programLabel}</span></div>
            <p className="control-copy">Choose what viewers see. Turning on a screen safely takes over from OBS while keeping enabled destinations connected.</p>
            <div className="screen-list">
              <div className="screen-item">
                <div><strong>BRB / connection lost</strong><span>{state.screens.brb.status === "ready" ? state.screens.brb.original_name || "Default screen ready" : state.screens.brb.status === "converting" ? "Converting on the VPS…" : state.screens.brb.status === "error" ? state.screens.brb.message : "Default screen unavailable"}</span></div>
                <div className="screen-item-controls"><label className={"upload-button " + (uploadBusy === "brb" ? "busy" : "")}>{uploadBusy === "brb" ? "Uploading…" : "Upload"}<input type="file" accept="video/*,.mov,.mkv,.mp4,.webm" disabled={Boolean(uploadBusy)} onChange={(event) => { uploadScreen("brb", event.target.files?.[0]); event.currentTarget.value = ""; }} /></label><button className={`toggle screen-toggle ${programMode === "brb" ? "on" : ""}`} type="button" role="switch" aria-checked={programMode === "brb"} aria-label={programMode === "brb" ? "Turn off BRB and return to live input" : "Put BRB on air"} disabled={protectionBusy || state.screens.brb.status !== "ready"} onClick={() => setPendingScreenMode(programMode === "brb" ? "live" : "brb")}><span /></button></div>
              </div>
              <div className="screen-item">
                <div><strong>Starting Soon</strong><span>{state.screens.starting_soon.status === "ready" ? state.screens.starting_soon.original_name || "Ready" : state.screens.starting_soon.status === "converting" ? "Converting on the VPS…" : state.screens.starting_soon.status === "error" ? state.screens.starting_soon.message : "Upload a video to enable"}</span></div>
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

        <section className="destinations" id="destinations">
          <div className="section-heading"><div><p className="eyebrow">OUTPUTS</p><h2>Destinations</h2></div><button className="secondary" type="button" onClick={() => setShowAdd(true)}>+ Add destination</button></div>
          {state.destinations.length ? <div className="destination-list">{state.destinations.map((destination) => (
            <div className="destination-row" key={destination.id} title={destination.last_error || undefined}>
              <PlatformIcon platform={destination.platform} fallback={destination.name[0]?.toUpperCase()} /><div className="destination-name"><strong>{destination.name}</strong><span>{audioModeLabel(destination.audio_track)}</span></div>
              <div className={`route-status ${destination.state === "forwarding" ? "online" : destination.enabled ? "pending" : ""}`}><span />{destination.state === "forwarding" ? "Forwarding" : destination.enabled ? "Connecting" : "Off"}</div>
              <button className={`toggle ${destination.enabled ? "on" : ""}`} type="button" role="switch" aria-checked={Boolean(destination.enabled)} aria-label={`${destination.enabled ? "Stop" : "Start"} ${destination.name} forwarding`} onClick={() => setPendingToggle(destination)}><span /></button>
            </div>))}</div> : <div className="empty-state"><strong>No destinations yet</strong><span>Add Twitch first, then YouTube with your clean audio track.</span><button onClick={() => setShowAdd(true)}>Add your first destination</button></div>}
        </section>

        {state.user.role === "owner" && <TeamPanel csrf={state.csrf} />}
        <section className="stats" aria-label="Stream statistics"><div><span>INPUT</span><strong>{isLive ? "ONLINE" : "STANDBY"}</strong></div><div><span>VIDEO</span><strong>H.264 copy</strong></div><div><span>AUDIO</span><strong>{Math.max(0, state.stream.media.tracks.length - 1)} tracks</strong></div><div><span>ACTIVE OUTPUTS</span><strong>{state.destinations.filter((item) => item.enabled).length}</strong></div></section>
      </section>

      {showOnboardingOffer && <Modal title="Want a quick Relay tour?" onClose={dismissOnboardingOffer}><div className="onboarding-offer"><div className="onboarding-spark">✦</div><p>Take a short walkthrough of OBS, destinations, and connection protection. You can reopen it anytime from <strong>Quick start</strong>.</p></div><div className="modal-actions"><button type="button" className="secondary" onClick={dismissOnboardingOffer}>Not now</button><button type="button" className="primary" onClick={() => { setShowOnboardingOffer(false); setShowOnboarding(true); }}>Start walkthrough</button></div></Modal>}
      {showOnboarding && <OnboardingTour state={state} onClose={() => { rememberOnboarding("dismissed"); setShowOnboarding(false); }} onFinish={finishOnboarding} />}
      {showObs && <Modal title="Connect OBS" onClose={() => setShowObs(false)}><p className="modal-copy">In OBS, choose <strong>Custom</strong> service, paste this into <strong>Server</strong>, and leave Stream Key blank.</p><label htmlFor="obs-url">Private SRT server address</label><div className="copy-field"><input id="obs-url" readOnly value={state.stream.obs_url} /><button onClick={copyObs}>{copied ? "Copied" : "Copy"}</button></div><p className="hint">Then select audio Tracks 1 and 2 under Advanced Output.</p></Modal>}
      {showAdd && <AddDestination csrf={state.csrf} twitchIngest={state.twitch_ingest} onClose={() => setShowAdd(false)} onAdded={async () => { setShowAdd(false); setState(await json<StreamState>("/api/state")); }} />}
      {pendingToggle && <Modal title={`${pendingToggle.enabled ? "Stop" : "Start"} ${pendingToggle.name}?`} onClose={() => { if (!toggleBusy) setPendingToggle(null); }}><p className="modal-copy">{pendingToggle.enabled ? `Relay will disconnect from ${pendingToggle.name}. Viewers there will lose the feed until you turn it on again.` : `Relay will immediately send your live input—or the protected backup screen—to ${pendingToggle.name}.`}</p><div className="confirm-destination"><PlatformIcon platform={pendingToggle.platform} fallback={pendingToggle.name[0]?.toUpperCase()} /><div><strong>{pendingToggle.name}</strong><span>{audioModeLabel(pendingToggle.audio_track)}</span></div></div><div className="modal-actions"><button type="button" className="secondary" disabled={toggleBusy} onClick={() => setPendingToggle(null)}>Cancel</button><button type="button" className={pendingToggle.enabled ? "danger" : "primary"} disabled={toggleBusy} onClick={confirmToggle}>{toggleBusy ? "Working…" : pendingToggle.enabled ? "Stop forwarding" : "Start forwarding"}</button></div></Modal>}
      {pendingScreenMode && <Modal title={pendingScreenMode === "live" ? "Return to live input?" : pendingScreenMode === "brb" ? "Put BRB on air?" : "Put Starting Soon on air?"} onClose={() => { if (!protectionBusy) setPendingScreenMode(null); }}><p className="modal-copy">{pendingScreenMode === "live" ? "Relay will allow OBS to reconnect. The BRB screen stays on air until the OBS signal arrives." : programMode === "live" ? "Relay will temporarily disconnect and hold OBS so this screen can feed every enabled destination. Automatic outage ads will not run during a manual screen." : "Relay will switch the current program screen while keeping enabled destinations connected."}</p><div className="confirm-destination screen-confirm"><div className="screen-confirm-icon">{pendingScreenMode === "live" ? "●" : pendingScreenMode === "brb" ? "B" : "S"}</div><div><strong>{pendingScreenMode === "live" ? "Live OBS input" : pendingScreenMode === "brb" ? "BRB / connection lost" : "Starting Soon"}</strong><span>{pendingScreenMode === "live" ? "OBS reconnect permitted" : "Manual program takeover"}</span></div></div><div className="modal-actions"><button type="button" className="secondary" disabled={protectionBusy} onClick={() => setPendingScreenMode(null)}>Cancel</button><button type="button" className="primary" disabled={protectionBusy} onClick={() => changeScreenMode(pendingScreenMode)}>{protectionBusy ? "Switching…" : pendingScreenMode === "live" ? "Return to live input" : "Put screen on air"}</button></div></Modal>}
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
      <ol className="tour-list"><li>Open <strong>Settings → Stream</strong> in OBS.</li><li>Choose <strong>Custom</strong> as the service.</li><li>Paste the address below into <strong>Server</strong> and leave <strong>Stream Key</strong> blank.</li><li>Under Advanced Output, enable streaming audio <strong>Tracks 1 and 2</strong>.</li></ol>
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

function Login({ onComplete }: { onComplete: () => Promise<void> }) {
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); setError(""); const data = new FormData(event.currentTarget); try { await json("/api/login", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(Object.fromEntries(data)) }); await onComplete(); } catch (err) { setError(err instanceof Error ? err.message : "Sign in failed"); } }
  return <AuthShell eyebrow="WELCOME BACK" title="Sign in to Relay" copy="Your streams and destinations are waiting."><form onSubmit={submit}><label>Username<input name="username" required autoCapitalize="none" /></label><label>Password<input name="password" type="password" required autoComplete="current-password" /></label>{error && <span className="form-error">{error}</span>}<button className="primary" type="submit">Sign in</button></form></AuthShell>;
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
        <div className="member-meta"><span>{member.destination_count} destination{member.destination_count === 1 ? "" : "s"}</span><span>{member.twitch_connected ? "Twitch connected" : "No Twitch login"}</span></div>
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

function Modal({ title, onClose, children }: { title:string; onClose:()=>void; children:React.ReactNode }) { return <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2><button aria-label="Close" onClick={onClose}>×</button></header>{children}</section></div>; }

function AddDestination({ csrf, twitchIngest, onClose, onAdded }: { csrf:string; twitchIngest:StreamState["twitch_ingest"]; onClose:()=>void; onAdded:()=>Promise<void> }) {
  const [platform, setPlatform] = useState("twitch");
  const [name, setName] = useState("Twitch");
  const [audioTrack, setAudioTrack] = useState("3");
  const [error, setError] = useState("");

  function choosePlatform(value: string) {
    const names: Record<string, string> = { twitch:"Twitch", youtube:"YouTube", rplay:"RPLAY", x:"X", custom:"Custom" };
    setPlatform(value);
    setName(names[value] || "Custom");
    setAudioTrack(value === "twitch" ? "3" : value === "youtube" ? "2" : "4");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    const data = Object.fromEntries(new FormData(event.currentTarget));
    try {
      await json("/api/destinations", {
        method:"POST",
        headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},
        body:JSON.stringify({ ...data, audio_track:Number(data.audio_track) }),
      });
      await onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save destination");
    }
  }

  const keyOnly = platform === "twitch" || platform === "youtube" || platform === "rplay" || platform === "x";
  const platformName = platform === "rplay" ? "RPLAY" : platform === "youtube" ? "YouTube" : platform === "x" ? "X" : "Twitch";

  return <Modal title="Add destination" onClose={onClose}><form onSubmit={submit}>
    <label>Platform<select name="platform" value={platform} onChange={(event) => choosePlatform(event.target.value)}><option value="twitch">Twitch</option><option value="youtube">YouTube</option><option value="rplay">RPLAY</option><option value="x">X</option><option value="custom">Custom RTMP</option></select></label>
    <label>Display name<input name="name" required value={name} onChange={(event) => setName(event.target.value)} /></label>
    <label>{keyOnly ? platformName + " stream key" : "Full RTMP address with stream key"}<input name="output_url" required type="password" autoComplete="off" placeholder={platform === "twitch" ? "live_…" : keyOnly ? "Paste " + platformName + " key" : "rtmps://…"} /></label>
    {platform === "twitch" && <p className="hint">Relay selects the ingest automatically. Current best: <strong>{twitchIngest.name}</strong>{twitchIngest.latency_ms != null ? " · " + Math.round(twitchIngest.latency_ms) + " ms" : ""}.</p>}
    {platform === "youtube" && <p className="hint">Relay sends matching copies to YouTube’s primary and backup ingests automatically.</p>}
    {platform === "rplay" && <p className="hint">Relay automatically sends this key through <strong>livestream-push.rplay.live</strong>.</p>}
    {platform === "x" && <p className="hint">Relay sends this key through <strong>ca.pscp.tv:80/x</strong>. Paste only the X stream key.</p>}
    <label>Audio mix<select name="audio_track" value={audioTrack} onChange={(event) => setAudioTrack(event.target.value)}>{platform === "twitch" && <option value="3">Live mix (Tracks 1 + 2) · clean VOD (Track 2)</option>}<option value="4">Combined mix — Tracks 1 + 2</option><option value="1">Track 1 only — music</option><option value="2">Track 2 only — clean/game</option></select></label>
    {platform === "twitch" && audioTrack === "3" && <p className="hint">Twitch viewers hear both tracks live. The saved VOD receives Track 2 without music.</p>}
    {error && <span className="form-error">{error}</span>}
    <div className="modal-actions"><button type="button" className="secondary" onClick={onClose}>Cancel</button><button className="primary" type="submit">Save destination</button></div>
  </form></Modal>;
}
