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
type RepoScope = { repository_id: string; github_repository?: string; aws_account_id?: string };
type Observer = { nodeStates: Partial<Record<GraphNodeId, LightState>>; jobs: QueueJob[]; pipeline: PipelineStep[] | null; repos: RepoScope[]; userProfiles: { email: string; profile: string | null }[] };
const OBS_DEFAULT: Observer = { nodeStates: {}, jobs: [], pipeline: null, repos: [], userProfiles: [] };

function useObserver() {
  const [obs, setObs] = useState<Observer>(OBS_DEFAULT);
  const api = useMemo(() => ({
    lightNode(node: GraphNodeId, state: LightState) { setObs(o => ({ ...o, nodeStates: { ...o.nodeStates, [node]: state } })); },
    upsertJob(job: QueueJob) { setObs(o => ({ ...o, jobs: [job, ...o.jobs.filter(j => j.id !== job.id)].slice(0, 8) })); },
    setPipeline(steps: PipelineStep[] | null) { setObs(o => ({ ...o, pipeline: steps })); },
    patchPipeline(key: string, state: LightState) { setObs(o => o.pipeline ? { ...o, pipeline: o.pipeline.map(s => s.key === key ? { ...s, state } : s) } : o); },
    setRepos(repos: RepoScope[]) { setObs(o => ({ ...o, repos })); },
    setUserProfiles(userProfiles: { email: string; profile: string | null }[]) { setObs(o => ({ ...o, userProfiles })); },
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
type Session = { accessToken: string; email: string; groups: string[]; sub: string; customerId: string | null; profile: string | null };
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
  return { accessToken: tok.access_token, email: String(claims["email"] ?? ""), groups, sub: String(claims["sub"] ?? ""), customerId: (claims["custom:customer_id"] as string) ?? null, profile: (claims["profile"] as string) ?? null };
}

/* =========================================================================
 * API helpers
 * =======================================================================*/
async function api<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, { ...init, headers: { ...(init?.body ? { "content-type": "application/json" } : {}), Authorization: `Bearer ${token}`, ...init?.headers } });
  if (!res.ok) {
    const d = await res.json().catch(() => null) as { code?: string; error?: { code?: string } } | null;
    const code = d?.error?.code ?? d?.code;
    throw new Error(code ? `${res.status} ${code}` : `요청 실패 (${res.status})`);
  }
  return res.json() as Promise<T>;
}
async function putPresigned(url: string, file: File, contentType: string) {
  const res = await fetch(url, { method: "PUT", headers: { "content-type": contentType }, body: file });
  if (!res.ok) throw new Error(`원본 업로드 실패 (${res.status})`);
}

/* per-user profile assignment + known profiles (client-side for the demo) */
const profKey = "gov.knownProfiles";
/* Per-user profile assignment now lives on the Cognito `profile` attribute (backend). Only the
 * published-profile list is kept client-side because there is no list-profiles API yet. */
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

    <div className="obs-title">연결된 고객사 리소스</div>
    {obs.repos.length === 0
      ? <div className="obs-empty">연결된 리소스가 없습니다.</div>
      : <div className="repo-list">{obs.repos.map(r => <div key={r.repository_id} className="repo-chip">
          <span className="light done" />
          <div className="repo-facts">
            <span className="q-label">{r.repository_id}</span>
            {r.github_repository && <span className="repo-fact"><span className="repo-k">GitHub</span><code>{r.github_repository}</code></span>}
            {r.aws_account_id && <span className="repo-fact"><span className="repo-k">AWS</span><code>{r.aws_account_id}</code></span>}
          </div>
        </div>)}</div>}

    <div className="obs-title">사용자 · 지정 Profile</div>
    {obs.userProfiles.length === 0
      ? <div className="obs-empty">등록된 사용자가 없습니다.</div>
      : obs.userProfiles.map(u => <div key={u.email} className="queue-item"><span className={`light ${u.profile ? "done" : "pending"}`} /><span className="q-label">{u.email}</span><span className="q-meta">{u.profile ?? "미지정"}</span></div>)}
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
function DocumentsPanel({ session, obs }: { session: Session; obs: ObserverApi }) {
  type Doc = { source_id: string; source_version: string; filename: string; status: string; source_format: string | null; byte_size: number | null; unit_count: number };
  type CartItem = { rule_id: string; rule_version: string; source_id: string; source_version: string; severity: string; control_key: string };
  const [docs, setDocs] = useState<Doc[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [mt, setMt] = useState(FORMATS[0].mt);
  const [running, setRunning] = useState(false);
  const [openDoc, setOpenDoc] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<Record<string, CandidatePage>>({});
  const [cart, setCart] = useState<CartItem[]>([]);
  const [profileId, setProfileId] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [repos, setRepos] = useState<RepoScope[]>([]);

  const refresh = async () => {
    try { const r = await api<{ sources: Doc[] }>("/policy-sources", session.accessToken); setDocs(r.sources); }
    catch (e) { setError((e as Error).message); }
  };
  const refreshScope = async () => {
    try { const r = await api<{ repositories: RepoScope[] }>("/scope", session.accessToken); setRepos(r.repositories); obs.setRepos(r.repositories); }
    catch { /* scope는 부가정보이므로 실패해도 문서 화면은 유지 */ }
  };
  useEffect(() => { void refresh(); void refreshScope(); /* eslint-disable-next-line */ }, []);

  async function deleteDoc(d: Doc) {
    setError(null); setNotice(null);
    if (!confirm(`문서 '${d.filename}' (${d.status})를 삭제할까요? S3 원본·정규화 아티팩트와 DynamoDB 기록이 영구 삭제됩니다.`)) return;
    const jobId = "del-" + d.source_id;
    obs.upsertJob({ id: jobId, label: `삭제 ${d.filename}`, queue: "policy-sources", state: "active" });
    try {
      await api(`/policy-sources/${enc(d.source_id)}/versions/${enc(d.source_version)}`, session.accessToken, { method: "DELETE" });
      obs.upsertJob({ id: jobId, label: `삭제 ${d.filename}`, queue: "policy-sources", state: "done", meta: "완료" });
      setNotice(`삭제됨: ${d.filename}`);
      await refresh();
    } catch (e) {
      const msg = (e as Error).message;
      const friendly = msg.includes("CONFLICT") ? "승인된 문서는 삭제할 수 없습니다(Profile이 참조 중)." : `삭제 실패: ${msg}`;
      obs.upsertJob({ id: jobId, label: `삭제 ${d.filename}`, queue: "policy-sources", state: "failed", meta: msg.includes("CONFLICT") ? "승인됨" : "실패" });
      setError(friendly);
    }
  }

  async function uploadAndExtract() {
    if (!file) { setError("파일을 선택하세요."); return; }
    setError(null); setNotice(null); setRunning(true);
    obs.setPipeline([
      { key: "upload", label: "1. 원본 업로드", state: "pending" },
      { key: "normalize", label: "2. 정규화", state: "pending" },
      { key: "extract", label: "3. 후보 추출 요청", state: "pending" },
      { key: "poll", label: "4. 후보 조회", state: "pending" },
    ]);
    obs.lightNode("authoring", "pending");
    try {
      obs.patchPipeline("upload", "active");
      const s = await api<UploadSession>("/policy-sources/uploads", session.accessToken, { method: "POST", body: JSON.stringify({ filename: file.name, declared_media_type: mt, byte_size: file.size }) });
      await putPresigned(s.upload_url, file, mt);
      obs.patchPipeline("upload", "done");
      obs.upsertJob({ id: "up-" + s.source_id, label: `업로드 ${file.name}`, queue: "s3", state: "done", meta: s.source_version.slice(0, 12) });
      obs.patchPipeline("normalize", "active");
      const doc = await api<NormalizedDoc>(`/policy-sources/${enc(s.source_id)}/versions/${enc(s.source_version)}/process`, session.accessToken, { method: "POST" });
      if (doc.status === "FAILED") { obs.patchPipeline("normalize", "failed"); throw new Error(`정규화 실패: ${doc.failure_code}`); }
      obs.patchPipeline("normalize", "done");
      obs.patchPipeline("extract", "active"); obs.lightNode("authoring", "active");
      obs.upsertJob({ id: "auth-" + s.source_id, label: "후보 추출", queue: "authoring", state: "active" });
      await api(`/policy-sources/${enc(s.source_id)}/versions/${enc(s.source_version)}/candidates`, session.accessToken, { method: "POST", body: "{}" });
      obs.patchPipeline("extract", "done");
      obs.patchPipeline("poll", "active");
      let result: CandidatePage | null = null;
      for (let i = 0; i < 40; i++) {
        await sleep(3000);
        let p: CandidatePage;
        try { p = await api<CandidatePage>(`/policy-sources/${enc(s.source_id)}/versions/${enc(s.source_version)}/candidates?limit=50`, session.accessToken); }
        catch { obs.upsertJob({ id: "auth-" + s.source_id, label: "후보 추출", queue: "authoring", state: "active", meta: `대기 중 (${i + 1}/40)` }); continue; }
        obs.upsertJob({ id: "auth-" + s.source_id, label: "후보 추출", queue: "authoring", state: "active", meta: `${p.status} (${i + 1}/40)` });
        if (p.candidates.length > 0 || (p.status !== "QUEUED" && p.status !== "RUNNING")) { result = p; break; }
      }
      if (!result) throw new Error("후보 추출이 2분 내에 완료되지 않았습니다. 잠시 후 문서 목록에서 '후보 조회'를 누르세요.");
      obs.patchPipeline("poll", "done"); obs.lightNode("authoring", "done");
      obs.upsertJob({ id: "auth-" + s.source_id, label: "후보 추출", queue: "authoring", state: "done", meta: `${result.candidates.length} 후보` });
      setCandidates(c => ({ ...c, [s.source_id]: result! }));
      setOpenDoc(s.source_id);
      setNotice(`추출 완료 · 후보 ${result.candidates.length}개. 필요한 후보를 담아 Profile로 만드세요.`);
      await refresh();
    } catch (e) { setError((e as Error).message); obs.lightNode("authoring", "failed"); }
    finally { setRunning(false); }
  }

  async function loadCandidates(d: Doc) {
    setError(null); setOpenDoc(d.source_id);
    try {
      const p = await api<CandidatePage>(`/policy-sources/${enc(d.source_id)}/versions/${enc(d.source_version)}/candidates?limit=50`, session.accessToken);
      setCandidates(c => ({ ...c, [d.source_id]: p }));
    } catch (e) { setError(`후보 조회 실패: ${(e as Error).message}`); }
  }

  const inCart = (rid: string, rv: string) => cart.some(i => i.rule_id === rid && i.rule_version === rv);
  function toggleCart(d: Doc, c: Candidate) {
    setCart(prev => inCart(c.rule_id, c.rule_version)
      ? prev.filter(i => !(i.rule_id === c.rule_id && i.rule_version === c.rule_version))
      : [...prev, { rule_id: c.rule_id, rule_version: c.rule_version, source_id: d.source_id, source_version: d.source_version, severity: c.proposed_severity, control_key: c.control_key }]);
  }

  async function publishCart() {
    if (cart.length === 0) { setError("장바구니에 후보를 담으세요."); return; }
    if (!profileId.trim()) { setError("Profile ID를 입력하세요."); return; }
    setError(null); setNotice(null);
    try {
      const bySource = new Map<string, { source_version: string; rules: { rule_id: string; version: string }[] }>();
      for (const i of cart) {
        const e = bySource.get(i.source_id) ?? { source_version: i.source_version, rules: [] };
        e.rules.push({ rule_id: i.rule_id, version: i.rule_version });
        bySource.set(i.source_id, e);
      }
      for (const [sid, e] of bySource) {
        await api(`/policy-sources/${enc(sid)}/versions/${enc(e.source_version)}/approve`, session.accessToken, { method: "POST", body: JSON.stringify({ approved_rules: e.rules }) });
      }
      const [firstSid, firstE] = [...bySource.entries()][0];
      await api("/policy-profiles", session.accessToken, { method: "POST", body: JSON.stringify({ source_id: firstSid, source_version: firstE.source_version, policy_profile_id: profileId.trim(), version: "v1" }) });
      addProfile(profileId.trim());
      setNotice(`Profile '${profileId.trim()}' 게시 완료 (${cart.length}개 rule, ${bySource.size}개 문서). '사용자 관리'에서 지정하세요.`);
      setCart([]);
    } catch (e) { setError(`게시 실패: ${(e as Error).message}`); }
  }

  return <div className="panel">
    <div className="card">
      <h2>정책 문서</h2>
      <p className="hint">문서를 업로드하면 정규화·후보추출까지 자동 진행됩니다(왼쪽 패널). 이미 추출한 문서는 다시 업로드하지 않고 '후보 조회'로 재사용합니다.</p>
      <div className="row" style={{ marginBottom: 4 }}>
        <span className="hint">연결된 고객사 리소스:</span>
        {repos.length === 0 ? <span className="hint">없음</span> : repos.map(r => <span key={r.repository_id} className="badge">{r.github_repository ? `GitHub ${r.github_repository}` : r.repository_id}{r.aws_account_id ? ` · AWS ${r.aws_account_id}` : ""}</span>)}
      </div>
      <div className="row">
        <label style={{ flex: 1 }}>형식<select value={mt} onChange={e => setMt(e.target.value)}>{FORMATS.map(f => <option key={f.mt} value={f.mt}>{f.label}</option>)}</select></label>
        <label style={{ flex: 2 }}>파일<input type="file" onChange={e => setFile(e.target.files?.[0] ?? null)} /></label>
        <button disabled={running || !file} onClick={() => void uploadAndExtract()}>{running ? "처리 중…" : "업로드 & 자동 추출"}</button>
      </div>
      <table><thead><tr><th>문서</th><th>상태</th><th>형식</th><th>단위</th><th></th></tr></thead>
        <tbody>{docs.map(d => <tr key={`${d.source_id}:${d.source_version}`}>
          <td>{d.filename}</td><td>{d.status}</td><td>{d.source_format ?? "-"}</td><td>{d.unit_count}</td>
          <td style={{ display: "flex", gap: 6 }}>
            <button className="ghost" onClick={() => void loadCandidates(d)}>후보 조회</button>
            <button className="ghost" style={{ borderColor: "var(--err)", color: "var(--err)" }} onClick={() => void deleteDoc(d)}>삭제</button>
          </td>
        </tr>)}
          {docs.length === 0 && <tr><td colSpan={5} className="obs-empty">업로드된 문서가 없습니다.</td></tr>}</tbody></table>
    </div>

    {openDoc && candidates[openDoc] && <div className="card">
      <h2>후보 — 필요한 것만 장바구니에 담기</h2>
      <p className="hint">심각도는 Catalog가 정한 읽기 전용 값입니다. 여러 문서에서 담아 하나의 Profile로 만들 수 있습니다.</p>
      {candidates[openDoc].candidates.length === 0 && <p className="obs-empty">후보 없음 (상태 {candidates[openDoc].status}).</p>}
      {candidates[openDoc].candidates.map(c => <div key={`${c.rule_id}@${c.rule_version}`} className="candidate">
        <label><input type="checkbox" checked={inCart(c.rule_id, c.rule_version)} onChange={() => toggleCart(docs.find(d => d.source_id === openDoc)!, c)} />
          <span><strong>{c.rule_id}@{c.rule_version}</strong><span className="badge">{c.proposed_severity}</span><span className="badge">{c.control_key}</span><br />{c.requirement_summary}<br /><span className="hint">{c.mapping_reason}</span></span></label>
      </div>)}
    </div>}

    <div className="card">
      <h2>Profile 장바구니 ({cart.length})</h2>
      {cart.length === 0 ? <p className="obs-empty">담긴 후보가 없습니다. 문서에서 후보를 조회해 담으세요.</p>
        : <table><thead><tr><th>Rule</th><th>Severity</th><th>Control</th><th>문서</th></tr></thead>
          <tbody>{cart.map(i => <tr key={`${i.rule_id}@${i.rule_version}`}><td>{i.rule_id}@{i.rule_version}</td><td>{i.severity}</td><td>{i.control_key}</td><td>{i.source_id.slice(0, 12)}</td></tr>)}</tbody></table>}
      <div className="row" style={{ marginTop: 12 }}>
        <label style={{ flex: 1 }}>Policy Profile ID<input value={profileId} onChange={e => setProfileId(e.target.value)} placeholder="profile-internal-baseline" /></label>
        <button disabled={cart.length === 0} onClick={() => void publishCart()}>장바구니로 Profile 게시</button>
      </div>
    </div>
    {notice && <p className="status">{notice}</p>}
    {error && <p className="alert">{error}</p>}
  </div>;
}

/* =========================================================================
 * Admin: user registration, list, and per-user profile assignment (backend)
 * =======================================================================*/
function UsersPanel({ session, obs }: { session: Session; obs: ObserverApi }) {
  type User = { username: string; email: string; customer_id: string; profile: string | null; status: string; enabled: boolean };
  const [users, setUsers] = useState<User[]>([]);
  const [profiles] = useState<string[]>(loadProfiles());
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("User");
  const [pw, setPw] = useState("");
  const [assignEmail, setAssignEmail] = useState("");
  const [assignPid, setAssignPid] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try { const r = await api<{ users: User[] }>("/admin/users", session.accessToken); setUsers(r.users); obs.setUserProfiles(r.users.map(u => ({ email: u.email, profile: u.profile }))); }
    catch (e) { setError((e as Error).message); }
  };
  useEffect(() => { void refresh(); /* eslint-disable-next-line */ }, []);

  async function createUser() {
    setError(null); setNotice(null);
    if (!email.trim() || pw.length < 8) { setError("이메일과 8자 이상 임시 비밀번호가 필요합니다."); return; }
    try {
      await api("/admin/users", session.accessToken, { method: "POST", body: JSON.stringify({ email: email.trim(), role, temporary_password: pw }) });
      setNotice(`사용자 생성: ${email.trim()} (${role})`); setEmail(""); setPw("");
      await refresh();
    } catch (e) { setError(`생성 실패: ${(e as Error).message}`); }
  }
  async function assign() {
    setError(null); setNotice(null);
    if (!assignEmail.trim() || !assignPid) { setError("사용자와 Profile을 선택하세요."); return; }
    try {
      await api("/admin/users/profile", session.accessToken, { method: "POST", body: JSON.stringify({ email: assignEmail.trim(), policy_profile_id: assignPid }) });
      setNotice(`${assignEmail.trim()} → ${assignPid} 지정 완료 (다음 로그인부터 적용)`);
      await refresh();
    } catch (e) { setError(`지정 실패: ${(e as Error).message}`); }
  }

  return <div className="panel">
    <div className="card">
      <h2>사용자 등록</h2>
      <p className="hint">고객(kosa-sandbox) 스코프의 사용자를 생성합니다. 임시 비밀번호는 대/소문자·숫자·기호 포함 8자 이상.</p>
      <div className="row">
        <label style={{ flex: 2 }}>이메일<input value={email} onChange={e => setEmail(e.target.value)} placeholder="user@example.com" /></label>
        <label style={{ flex: 1 }}>역할<select value={role} onChange={e => setRole(e.target.value)}><option>User</option><option>Admin</option></select></label>
        <label style={{ flex: 2 }}>임시 비밀번호<input type="text" value={pw} onChange={e => setPw(e.target.value)} placeholder="Temp!2026" /></label>
        <button onClick={() => void createUser()}>등록</button>
      </div>
    </div>
    <div className="card">
      <h2>사용자 목록 · Profile 지정</h2>
      <div className="row">
        <label style={{ flex: 2 }}>사용자<select value={assignEmail} onChange={e => setAssignEmail(e.target.value)}><option value="">선택</option>{users.map(u => <option key={u.username} value={u.email}>{u.email}</option>)}</select></label>
        <label style={{ flex: 2 }}>Profile<select value={assignPid} onChange={e => setAssignPid(e.target.value)}><option value="">선택</option>{profiles.map(p => <option key={p} value={p}>{p}</option>)}</select></label>
        <button onClick={() => void assign()}>지정</button>
      </div>
      <table><thead><tr><th>이메일</th><th>역할/상태</th><th>지정 Profile</th></tr></thead>
        <tbody>{users.map(u => <tr key={u.username}><td>{u.email}</td><td>{u.status}{u.enabled ? "" : " (비활성)"}</td><td>{u.profile ? <code>{u.profile}</code> : "-"}</td></tr>)}
          {users.length === 0 && <tr><td colSpan={3} className="obs-empty">사용자가 없습니다.</td></tr>}</tbody></table>
      <p className="hint">Profile 목록은 이 브라우저에서 게시한 것을 보여줍니다(백엔드 list-profiles 미구현).</p>
    </div>
    {notice && <p className="status">{notice}</p>}
    {error && <p className="alert">{error}</p>}
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
type View = "chat" | "documents" | "users" | "report";
function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("chat");
  const [assessmentId, setAssessmentId] = useState<string | null>(null);
  const observer = useObserver();
  useEffect(() => { exchangeCallback().then(s => { if (s) setSession(s); }).catch(e => setError((e as Error).message)); }, []);
  const isAdmin = !!session?.groups.includes("Admin");
  useEffect(() => {
    if (!session || !isAdmin) return;
    void (async () => {
      try { const r = await api<{ repositories: RepoScope[] }>("/scope", session.accessToken); observer.setRepos(r.repositories); } catch { /* keep panel */ }
      try { const r = await api<{ users: { email: string; profile: string | null }[] }>("/admin/users", session.accessToken); observer.setUserProfiles(r.users.map(u => ({ email: u.email, profile: u.profile }))); } catch { /* keep panel */ }
    })();
    /* eslint-disable-next-line */
  }, [session, isAdmin]);
  if (!session) return <Login error={error} />;
  const myProfile = session.profile;
  const nav: { id: View; label: string; admin?: boolean }[] = [
    { id: "chat", label: "챗봇" },
    { id: "documents", label: "정책 문서", admin: true },
    { id: "users", label: "사용자 관리", admin: true },
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
        <span className="who">{session.email}{myProfile ? ` · profile: ${myProfile}` : ""}</span>
      </div>
      {view === "chat" && <Chat session={session} obs={observer} profileId={myProfile} onAssessment={id => { setAssessmentId(id); setView("report"); }} />}
      {view === "documents" && isAdmin && <DocumentsPanel session={session} obs={observer} />}
      {view === "users" && isAdmin && <UsersPanel session={session} obs={observer} />}
      {view === "report" && assessmentId && <ReportPanel session={session} assessmentId={assessmentId} />}
    </div>
  </div>;
}
createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
