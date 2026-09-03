import { StrictMode, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

/* =========================================================================
 * Config
 * =======================================================================*/
const API = import.meta.env.VITE_API_BASE_URL ?? "";
const COGNITO_DOMAIN = import.meta.env.VITE_COGNITO_DOMAIN as string;
const COGNITO_CLIENT_ID = import.meta.env.VITE_COGNITO_CLIENT_ID as string;
const REDIRECT_URI = (import.meta.env.VITE_COGNITO_REDIRECT_URI as string) ?? window.location.origin;
const enc = encodeURIComponent;
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

/* =========================================================================
 * Types (API wire shapes)
 * =======================================================================*/
type OrchestrationDecision = { intent: string; rationale: string; answer: string | null; selector: (Record<string, unknown> & { repository_id?: string; policy_profile_id?: string }) | null; requires_confirmation: boolean };
type NormalizedDoc = { source_id: string; source_version: string; status: string; source_format: string | null; byte_size: number; units: { locator: string }[]; warnings: string[]; failure_code: string | null };
type UploadSession = { source_id: string; source_version: string; upload_url: string };
type Candidate = { rule_id: string; rule_version: string; classification: string; requirement_summary: string; mapping_reason: string; control_key: string; evaluation_type: string; proposed_severity: string; resource_types: string[] };
type CandidatePage = { status: string; counts: Record<string, number> | null; candidates: Candidate[]; unsupported: { requirement_summary?: string; requirement?: string }[]; rejected: unknown[]; cursor: string | null };
type Report = { assessment_id: string; results: { resource_id: string; rule_id: string; perspective: string; status: string; score: number }[]; findings: { finding_id: string; resource_id: string; rule_id: string; status: string; severity: string; score: number; rationale: string }[]; readiness_score: { score: number } | null; coverage: { percentage: number; completed_evaluations: number; planned_evaluations: number } };

/* =========================================================================
 * Observability store — the whole point: show what's happening inside.
 * =======================================================================*/
type LightState = "pending" | "active" | "done" | "failed";
type GraphNodeId = "parent" | "policy_qa" | "assessment" | "remediation" | "deployment" | "authoring";
type QueueJob = { id: string; label: string; queue: string; state: LightState; meta?: string };
type PipelineStep = { key: string; label: string; state: LightState };
type Observer = { nodeStates: Partial<Record<GraphNodeId, LightState>>; jobs: QueueJob[]; pipeline: PipelineStep[] | null };
const OBS_DEFAULT: Observer = { nodeStates: {}, jobs: [], pipeline: null };

function useObserver() {
  const [obs, setObs] = useState<Observer>(OBS_DEFAULT);
  const api = useMemo(() => ({
    lightNode(node: GraphNodeId, state: LightState) { setObs(o => ({ ...o, nodeStates: { ...o.nodeStates, [node]: state } })); },
    upsertJob(job: QueueJob) { setObs(o => ({ ...o, jobs: [job, ...o.jobs.filter(j => j.id !== job.id)].slice(0, 8) })); },
    setPipeline(steps: PipelineStep[] | null) { setObs(o => ({ ...o, pipeline: steps })); },
    patchPipeline(key: string, state: LightState) { setObs(o => o.pipeline ? { ...o, pipeline: o.pipeline.map(s => s.key === key ? { ...s, state } : s) } : o); },
  }), []);
  return { obs, ...api };
}
type ObserverApi = ReturnType<typeof useObserver>;

/* =========================================================================
 * Auth — Cognito Hosted UI PKCE; role from cognito:groups in the ID token.
 * =======================================================================*/
const verifierKey = "gov.pkce.verifier";
const stateKey = "gov.pkce.state";
function b64url(bytes: Uint8Array) { return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", ""); }
async function sha256(v: string) { return b64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(v)))); }
async function startLogin() {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
  sessionStorage.setItem(verifierKey, verifier);
  sessionStorage.setItem(stateKey, state);
  const q = new URLSearchParams({ client_id: COGNITO_CLIENT_ID, response_type: "code", scope: "openid email", redirect_uri: REDIRECT_URI, state, code_challenge_method: "S256", code_challenge: await sha256(verifier) });
  window.location.assign(`https://${COGNITO_DOMAIN}/oauth2/authorize?${q}`);
}
type Session = { accessToken: string; email: string; groups: string[]; sub: string; customerId: string | null };
function decodeJwt(token: string): Record<string, unknown> {
  const json = atob(token.split(".")[1].replaceAll("-", "+").replaceAll("_", "/"));
  return JSON.parse(decodeURIComponent(escape(json)));
}
async function exchangeCallback(): Promise<Session | null> {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  if (!code) return null;
  if (params.get("state") !== sessionStorage.getItem(stateKey)) throw new Error("로그인 state 불일치");
  const verifier = sessionStorage.getItem(verifierKey);
  if (!verifier) throw new Error("로그인 verifier 없음");
  const body = new URLSearchParams({ grant_type: "authorization_code", client_id: COGNITO_CLIENT_ID, code, redirect_uri: REDIRECT_URI, code_verifier: verifier });
  const res = await fetch(`https://${COGNITO_DOMAIN}/oauth2/token`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body });
  if (!res.ok) throw new Error("토큰 교환 실패");
  const tok = await res.json() as { access_token?: string; id_token?: string };
  if (!tok.access_token || !tok.id_token) throw new Error("토큰 없음");
  sessionStorage.removeItem(verifierKey); sessionStorage.removeItem(stateKey);
  history.replaceState({}, "", window.location.pathname);
  const claims = decodeJwt(tok.id_token);
  const groups = Array.isArray(claims["cognito:groups"]) ? (claims["cognito:groups"] as string[]) : [];
  return { accessToken: tok.access_token, email: String(claims["email"] ?? ""), groups, sub: String(claims["sub"] ?? ""), customerId: (claims["custom:customer_id"] as string) ?? null };
}

/* =========================================================================
 * API helpers
 * =======================================================================*/
async function api<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, { ...init, headers: { ...(init?.body ? { "content-type": "application/json" } : {}), Authorization: `Bearer ${token}`, ...init?.headers } });
  if (!res.ok) { const d = await res.json().catch(() => null) as { code?: string } | null; throw new Error(d?.code ? `${res.status} ${d.code}` : `요청 실패 (${res.status})`); }
  return res.json() as Promise<T>;
}
async function putPresigned(url: string, file: File, contentType: string) {
  const res = await fetch(url, { method: "PUT", headers: { "content-type": contentType }, body: file });
  if (!res.ok) throw new Error(`원본 업로드 실패 (${res.status})`);
}

/* per-user profile assignment + known profiles (client-side for the demo) */
const assignKey = "gov.userProfiles";
const profKey = "gov.knownProfiles";
function loadAssignments(): Record<string, string> { try { return JSON.parse(localStorage.getItem(assignKey) ?? "{}"); } catch { return {}; } }
function saveAssignment(email: string, pid: string) { const a = loadAssignments(); a[email] = pid; localStorage.setItem(assignKey, JSON.stringify(a)); }
function loadProfiles(): string[] { try { return JSON.parse(localStorage.getItem(profKey) ?? "[]"); } catch { return []; } }
function addProfile(id: string) { const p = new Set(loadProfiles()); p.add(id); localStorage.setItem(profKey, JSON.stringify([...p])); }

/* =========================================================================
 * Left observability panel
 * =======================================================================*/
const GRAPH: { id: GraphNodeId; name: string; sub?: boolean; note?: string }[] = [
  { id: "parent", name: "Parent Orchestrator", note: "자연어 라우팅" },
  { id: "policy_qa", name: "Policy Q&A", sub: true },
  { id: "assessment", name: "Assessment", sub: true, note: "IAC·ACTUAL·DRIFT" },
  { id: "remediation", name: "Remediation", sub: true, note: "patch·sync" },
  { id: "deployment", name: "Deployment", sub: true, note: "plan·apply·verify" },
  { id: "authoring", name: "Policy Authoring", sub: true, note: "rule 후보" },
];
function ObserverPanel({ obs }: { obs: Observer }) {
  return <aside className="observer">
    <div className="obs-title">LangGraph</div>
    <div className="graph">
      {GRAPH.map(n => {
        const st = obs.nodeStates[n.id] ?? "";
        return <div key={n.id} className={["node", n.sub ? "sub" : "parent", st].join(" ")}>
          {n.sub && <span className="edge" />}<span className="dot" /><span className="n-name">{n.name}</span>{n.note && <span className="n-sub">{n.note}</span>}
        </div>;
      })}
    </div>
    {obs.pipeline && <>
      <div className="obs-title">문서 파이프라인</div>
      <div className="stepper">{obs.pipeline.map(s => <div key={s.key} className={`step ${s.state}`}><span className={`light ${s.state}`} />{s.label}</div>)}</div>
    </>}
    <div className="obs-title">Queue / Jobs</div>
    {obs.jobs.length === 0 && <div className="obs-empty">진행 중인 작업이 없습니다.</div>}
    {obs.jobs.map(j => <div key={j.id} className="queue-item"><span className={`light ${j.state}`} /><span className="q-label">{j.label}</span><span className="q-meta">{j.meta ?? j.queue}</span></div>)}
  </aside>;
}

/* =========================================================================
 * Login (role split)
 * =======================================================================*/
function Login({ error }: { error: string | null }) {
  return <div className="login-wrap"><div className="login-card">
    <h1>Cloud Governance Console</h1>
    <p>역할을 선택해 로그인하세요. 실제 권한은 로그인 후 토큰의 그룹으로 결정됩니다.</p>
    <div className="role-btns">
      <button className="role-btn" onClick={() => void startLogin()}><span className="r-title">관리자로 로그인</span><span className="r-desc">문서 업로드 · Profile 생성 · 사용자별 Profile 지정 · 평가/조치/배포</span></button>
      <button className="role-btn" onClick={() => void startLogin()}><span className="r-title">일반 사용자로 로그인</span><span className="r-desc">챗봇으로 평가 요청 · 지정된 Profile 확인 · Finding 검토</span></button>
    </div>
    {error && <p className="alert" style={{ marginTop: 16 }}>{error}</p>}
  </div></div>;
}

/* =========================================================================
 * Chat (Parent Orchestrator)
 * =======================================================================*/
type Turn = { role: "user" | "bot"; text: string; decision?: OrchestrationDecision };
function Chat({ session, obs, profileId, onAssessment }: { session: Session; obs: ObserverApi; profileId: string | null; onAssessment: (id: string) => void }) {
  const [turns, setTurns] = useState<Turn[]>([{ role: "bot", text: "무엇을 도와드릴까요? 예: \"test 리포지토리를 우리 정책으로 평가해줘\" 또는 정책 관련 질문." }]);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight); }, [turns]);

  async function send() {
    const text = msg.trim();
    if (!text || busy) return;
    setMsg(""); setBusy(true);
    setTurns(t => [...t, { role: "user", text }]);
    obs.lightNode("parent", "active");
    try {
      const d = await api<OrchestrationDecision>("/orchestrate", session.accessToken, { method: "POST", body: JSON.stringify({ message: text }) });
      obs.lightNode("parent", "done");
      const sub: Record<string, GraphNodeId> = { POLICY_QA: "policy_qa", ASSESSMENT: "assessment", REMEDIATION: "remediation", DEPLOYMENT: "deployment" };
      const node = sub[d.intent];
      if (node) obs.lightNode(node, d.intent === "POLICY_QA" ? "done" : "pending");
      setTurns(t => [...t, { role: "bot", text: d.answer ?? d.rationale, decision: d }]);
    } catch (e) { obs.lightNode("parent", "failed"); setTurns(t => [...t, { role: "bot", text: `오류: ${(e as Error).message}` }]); }
    finally { setBusy(false); }
  }
  async function confirmAssessment(sel: Record<string, unknown> & { repository_id?: string }) {
    obs.lightNode("assessment", "active");
    obs.upsertJob({ id: "assess-" + Date.now(), label: "Assessment 시작", queue: "assessment", state: "active" });
    try {
      const r = await api<{ assessment_id?: string }>("/assessments", session.accessToken, { method: "POST", body: JSON.stringify({ repository_id: sel.repository_id, policy_profile_id: profileId ?? sel.policy_profile_id }) });
      if (!r.assessment_id) throw new Error("assessment_id 없음");
      obs.lightNode("assessment", "done");
      onAssessment(r.assessment_id);
    } catch (e) { obs.lightNode("assessment", "failed"); setTurns(t => [...t, { role: "bot", text: `평가 시작 실패: ${(e as Error).message}` }]); }
  }
  return <div className="chat">
    <div className="chat-log" ref={logRef}>
      {turns.map((t, i) => <div key={i} className={`msg ${t.role === "user" ? "user" : ""}`}>
        <div className={`avatar ${t.role === "user" ? "me" : "bot"}`}>{t.role === "user" ? "나" : "AI"}</div>
        <div className="bubble">
          {t.decision && <span className="intent-tag">{t.decision.intent}</span>}
          <div>{t.text}</div>
          {t.decision?.intent === "ASSESSMENT" && t.decision.selector && <div className="confirm">
            <div>제안 범위: repository <code>{t.decision.selector.repository_id ?? "?"}</code> · profile <code>{profileId ?? t.decision.selector.policy_profile_id ?? "미지정"}</code></div>
            <button style={{ marginTop: 8 }} onClick={() => void confirmAssessment(t.decision!.selector!)}>이 Assessment 시작</button>
          </div>}
          {t.decision?.requires_confirmation && t.decision.intent !== "ASSESSMENT" && <div className="confirm"><em>{t.decision.intent} 제안은 해당 화면에서 확인 후 실행합니다.</em></div>}
        </div>
      </div>)}
    </div>
    <div className="chat-input">
      <input value={msg} onChange={e => setMsg(e.target.value)} onKeyDown={e => { if (e.key === "Enter") void send(); }} placeholder="메시지를 입력하세요…" />
      <button disabled={busy} onClick={() => void send()}>{busy ? "…" : "보내기"}</button>
    </div>
  </div>;
}

/* =========================================================================
 * Admin: upload with AUTOMATIC pipeline (normalize→extract→poll), select only.
 * =======================================================================*/
const FORMATS = [
  { label: "Markdown", mt: "text/markdown" },
  { label: "Plain text", mt: "text/plain" },
  { label: "CSV", mt: "text/csv" },
  { label: "XLSX", mt: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
  { label: "DOCX", mt: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" },
];
function UploadPanel({ session, obs }: { session: Session; obs: ObserverApi }) {
  const [file, setFile] = useState<File | null>(null);
  const [mt, setMt] = useState(FORMATS[0].mt);
  const [sess, setSess] = useState<UploadSession | null>(null);
  const [page, setPage] = useState<CandidatePage | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [profileId, setProfileId] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const lastFile = useRef<string>("");
  const key = (c: Candidate) => `${c.rule_id}@${c.rule_version}`;

  async function runPipeline() {
    if (!file) { setError("파일을 선택하세요."); return; }
    setError(null); setNotice(null); setPage(null); setSelected(new Set()); setRunning(true);
    obs.setPipeline([
      { key: "upload", label: "1. 원본 업로드", state: "pending" },
      { key: "normalize", label: "2. 정규화", state: "pending" },
      { key: "extract", label: "3. 후보 추출 요청", state: "pending" },
      { key: "poll", label: "4. 후보 조회", state: "pending" },
    ]);
    obs.lightNode("authoring", "pending");
    if (lastFile.current === `${file.name}:${file.size}`) setNotice("동일한 파일입니다. 새 업로드 세션(새 source version)으로 다시 처리합니다.");
    lastFile.current = `${file.name}:${file.size}`;
    try {
      obs.patchPipeline("upload", "active");
      const s = await api<UploadSession>("/policy-sources/uploads", session.accessToken, { method: "POST", body: JSON.stringify({ filename: file.name, declared_media_type: mt, byte_size: file.size }) });
      await putPresigned(s.upload_url, file, mt);
      setSess(s); obs.patchPipeline("upload", "done");
      obs.upsertJob({ id: "up-" + s.source_id, label: `업로드 ${file.name}`, queue: "s3", state: "done", meta: s.source_version.slice(0, 12) });

      obs.patchPipeline("normalize", "active");
      const doc = await api<NormalizedDoc>(`/policy-sources/${enc(s.source_id)}/versions/${enc(s.source_version)}/process`, session.accessToken, { method: "POST" });
      if (doc.status === "FAILED") { obs.patchPipeline("normalize", "failed"); throw new Error(`정규화 실패: ${doc.failure_code}`); }
      obs.patchPipeline("normalize", "done");
      setNotice(`정규화 완료 · 형식 ${doc.source_format} · 단위 ${doc.units.length}개`);

      obs.patchPipeline("extract", "active");
      obs.lightNode("authoring", "active");
      obs.upsertJob({ id: "auth-" + s.source_id, label: "정책 후보 추출", queue: "authoring", state: "active" });
      await api(`/policy-sources/${enc(s.source_id)}/versions/${enc(s.source_version)}/candidates`, session.accessToken, { method: "POST", body: "{}" });
      obs.patchPipeline("extract", "done");

      obs.patchPipeline("poll", "active");
      // A안: 서버가 브라우저로 push하지 않으므로, DynamoDB에 남는 실행 상태(QUEUED/RUNNING/READY)를
      // 조회해 왼쪽 패널에 그대로 비춘다. manifest가 아직 없으면 API가 일시적으로 실패(503)하는데,
      // 이는 "아직 시작 전"이라는 정상 상태이므로 실패로 처리하지 않고 계속 기다린다.
      let result: CandidatePage | null = null;
      const maxTries = 40; // 40 × 3s = 최대 2분
      for (let i = 0; i < maxTries; i++) {
        await sleep(3000);
        let p: CandidatePage;
        try {
          p = await api<CandidatePage>(`/policy-sources/${enc(s.source_id)}/versions/${enc(s.source_version)}/candidates?limit=50`, session.accessToken);
        } catch {
          obs.upsertJob({ id: "auth-" + s.source_id, label: "정책 후보 추출", queue: "authoring", state: "active", meta: `대기 중… (${i + 1}/${maxTries})` });
          continue; // manifest 미생성 — 계속 대기
        }
        obs.upsertJob({ id: "auth-" + s.source_id, label: "정책 후보 추출", queue: "authoring", state: "active", meta: `${p.status} (${i + 1}/${maxTries})` });
        if (p.candidates.length > 0 || (p.status !== "QUEUED" && p.status !== "RUNNING")) { result = p; break; }
      }
      if (!result) throw new Error("후보 추출이 2분 내에 완료되지 않았습니다. 문서가 크면 더 걸릴 수 있으니 잠시 후 다시 업로드하세요.");
      setPage(result); obs.patchPipeline("poll", "done"); obs.lightNode("authoring", "done");
      obs.upsertJob({ id: "auth-" + s.source_id, label: "정책 후보 추출", queue: "authoring", state: "done", meta: `${result.candidates.length} 후보` });
      setNotice(`후보 ${result.candidates.length}개 · 미지원 ${result.unsupported.length} · 검토 후 승인하세요.`);
    } catch (e) { setError((e as Error).message); obs.lightNode("authoring", "failed"); }
    finally { setRunning(false); }
  }
  function toggle(c: Candidate) { setSelected(p => { const n = new Set(p); const k = key(c); n.has(k) ? n.delete(k) : n.add(k); return n; }); }
  async function approveAndPublish() {
    if (!sess || !page) return;
    if (selected.size === 0) { setError("승인할 후보를 선택하세요."); return; }
    if (!profileId.trim()) { setError("Profile ID를 입력하세요."); return; }
    setError(null);
    try {
      const approved = page.candidates.filter(c => selected.has(key(c))).map(c => ({ rule_id: c.rule_id, version: c.rule_version }));
      await api(`/policy-sources/${enc(sess.source_id)}/versions/${enc(sess.source_version)}/approve`, session.accessToken, { method: "POST", body: JSON.stringify({ approved_rules: approved }) });
      await api("/policy-profiles", session.accessToken, { method: "POST", body: JSON.stringify({ source_id: sess.source_id, source_version: sess.source_version, policy_profile_id: profileId.trim(), version: "v1" }) });
      addProfile(profileId.trim());
      setNotice(`Profile 게시 완료: ${profileId.trim()} — 고객(kosa-sandbox) Catalog에 저장. 'Profile·사용자 관리'에서 사용자에게 지정하세요.`);
    } catch (e) { setError((e as Error).message); }
  }
  return <div className="panel">
    <div className="card">
      <h2>정책 문서 업로드</h2>
      <p className="hint">업로드하면 정규화 → 후보 추출 → 조회까지 자동 진행됩니다. 왼쪽 패널에서 각 단계 불빛을 볼 수 있습니다. 사람은 후보 선택과 게시만 합니다.</p>
      <div className="row">
        <label style={{ flex: 1 }}>형식<select value={mt} onChange={e => setMt(e.target.value)}>{FORMATS.map(f => <option key={f.mt} value={f.mt}>{f.label}</option>)}</select></label>
        <label style={{ flex: 2 }}>파일<input type="file" onChange={e => setFile(e.target.files?.[0] ?? null)} /></label>
      </div>
      <button disabled={running || !file} onClick={() => void runPipeline()}>{running ? "처리 중…" : "업로드 & 자동 처리"}</button>
    </div>
    {page && <div className="card">
      <h2>후보 검토 · 승인</h2>
      <p className="hint">심각도(severity)는 Catalog가 정한 읽기 전용 값입니다. 승인할 항목만 선택하세요.</p>
      {page.candidates.length === 0 && <p className="obs-empty">표시할 후보가 없습니다 (상태 {page.status}).</p>}
      {page.candidates.map(c => <div key={key(c)} className="candidate">
        <label><input type="checkbox" checked={selected.has(key(c))} onChange={() => toggle(c)} />
          <span><strong>{c.rule_id}@{c.rule_version}</strong><span className="badge">{c.proposed_severity}</span><span className="badge">{c.control_key}</span><br />{c.requirement_summary}<br /><span className="hint">{c.mapping_reason}</span></span></label>
      </div>)}
      <div className="row" style={{ marginTop: 12 }}>
        <label style={{ flex: 1 }}>Policy Profile ID<input value={profileId} onChange={e => setProfileId(e.target.value)} placeholder="profile-internal-baseline" /></label>
        <button disabled={selected.size === 0} onClick={() => void approveAndPublish()}>선택 {selected.size}개 승인 &amp; Profile 게시</button>
      </div>
    </div>}
    {notice && <p className="status">{notice}</p>}
    {error && <p className="alert">{error}</p>}
  </div>;
}

/* =========================================================================
 * Admin: profiles + per-user profile assignment
 * =======================================================================*/
function ProfilesPanel() {
  const [profiles, setProfiles] = useState<string[]>(loadProfiles());
  const [assignments, setAssignments] = useState<Record<string, string>>(loadAssignments());
  const [email, setEmail] = useState("");
  const [pid, setPid] = useState("");
  const [newP, setNewP] = useState("");
  function assign() { if (!email.trim() || !pid) return; saveAssignment(email.trim(), pid); setAssignments(loadAssignments()); }
  function register() { if (!newP.trim()) return; addProfile(newP.trim()); setProfiles(loadProfiles()); setNewP(""); }
  return <div className="panel">
    <div className="card">
      <h2>Policy Profile 목록</h2>
      <p className="hint">게시된 Profile은 고객(kosa-sandbox) Catalog에 저장됩니다. Assessment는 생성 시 이 중 하나의 버전을 고정합니다.</p>
      {profiles.length === 0 ? <p className="obs-empty">아직 게시된 Profile이 없습니다. '문서 업로드'에서 만드세요.</p> : <ul>{profiles.map(p => <li key={p}><code>{p}</code></li>)}</ul>}
      <div className="row"><label style={{ flex: 1 }}>Profile ID 직접 등록<input value={newP} onChange={e => setNewP(e.target.value)} /></label><button className="ghost" onClick={register}>목록에 추가</button></div>
    </div>
    <div className="card">
      <h2>사용자별 Profile 지정</h2>
      <p className="hint">관리자가 사용자마다 기본 Profile을 지정합니다. 해당 사용자가 로그인하면 챗봇의 Assessment 제안에 이 Profile이 적용됩니다.</p>
      <div className="row">
        <label style={{ flex: 1 }}>사용자 이메일<input value={email} onChange={e => setEmail(e.target.value)} placeholder="user@example.com" /></label>
        <label style={{ flex: 1 }}>Profile<select value={pid} onChange={e => setPid(e.target.value)}><option value="">선택</option>{profiles.map(p => <option key={p} value={p}>{p}</option>)}</select></label>
        <button onClick={assign}>지정</button>
      </div>
      <table><thead><tr><th>사용자</th><th>지정된 Profile</th></tr></thead>
        <tbody>{Object.entries(assignments).map(([e, p]) => <tr key={e}><td>{e}</td><td><code>{p}</code></td></tr>)}
          {Object.keys(assignments).length === 0 && <tr><td colSpan={2} className="obs-empty">지정된 사용자가 없습니다.</td></tr>}</tbody></table>
    </div>
  </div>;
}

/* =========================================================================
 * Assessment report
 * =======================================================================*/
function ReportPanel({ session, assessmentId }: { session: Session; assessmentId: string }) {
  const [rep, setRep] = useState<Report | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api<Report>(`/assessments/${enc(assessmentId)}?limit=50`, session.accessToken).then(setRep).catch(e => setErr((e as Error).message)); }, [assessmentId, session.accessToken]);
  if (err) return <div className="panel"><div className="card"><p className="alert">{err}</p></div></div>;
  if (!rep) return <div className="panel"><div className="card"><p className="obs-empty">Assessment 결과를 불러오는 중…</p></div></div>;
  return <div className="panel">
    <div className="card"><h2>Assessment 결과</h2>
      <div className="row"><span>실행률 <strong>{rep.coverage.percentage}%</strong> ({rep.coverage.completed_evaluations}/{rep.coverage.planned_evaluations})</span><span>Readiness <strong>{rep.readiness_score?.score ?? "계산 대기"}</strong></span></div>
    </div>
    <div className="card"><h2>Findings ({rep.findings.length})</h2>
      {rep.findings.map(f => <div key={f.finding_id} className="candidate"><strong>{f.severity}</strong> · {f.status} · {f.resource_id} · {f.rule_id} · score {f.score}<br /><span className="hint">{f.rationale}</span></div>)}
      {rep.findings.length === 0 && <p className="obs-empty">Finding이 없습니다.</p>}
    </div>
  </div>;
}

/* =========================================================================
 * App
 * =======================================================================*/
type View = "chat" | "upload" | "profiles" | "report";
function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("chat");
  const [assessmentId, setAssessmentId] = useState<string | null>(null);
  const observer = useObserver();
  useEffect(() => { exchangeCallback().then(s => { if (s) setSession(s); }).catch(e => setError((e as Error).message)); }, []);
  if (!session) return <Login error={error} />;
  const isAdmin = session.groups.includes("Admin");
  const myProfile = loadAssignments()[session.email] ?? null;
  const nav: { id: View; label: string; admin?: boolean }[] = [
    { id: "chat", label: "챗봇" },
    { id: "upload", label: "문서 업로드", admin: true },
    { id: "profiles", label: "Profile · 사용자 관리", admin: true },
  ];
  return <div className="shell">
    <ObserverPanel obs={observer.obs} />
    <div className="workspace">
      <div className="topbar">
        <span className="brand">Cloud Governance</span>
        <span className={`role-chip ${isAdmin ? "admin" : ""}`}>{isAdmin ? "관리자" : "사용자"}</span>
        <nav>
          {nav.filter(n => !n.admin || isAdmin).map(n => <button key={n.id} className={view === n.id ? "active" : ""} onClick={() => setView(n.id)}>{n.label}</button>)}
          {assessmentId && <button className={view === "report" ? "active" : ""} onClick={() => setView("report")}>Assessment 결과</button>}
        </nav>
        <span className="spacer" />
        <span className="who">{session.email}{myProfile && !isAdmin ? ` · profile: ${myProfile}` : ""}</span>
      </div>
      {view === "chat" && <Chat session={session} obs={observer} profileId={myProfile} onAssessment={id => { setAssessmentId(id); setView("report"); }} />}
      {view === "upload" && isAdmin && <UploadPanel session={session} obs={observer} />}
      {view === "profiles" && isAdmin && <ProfilesPanel />}
      {view === "report" && assessmentId && <ReportPanel session={session} assessmentId={assessmentId} />}
    </div>
  </div>;
}
createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
