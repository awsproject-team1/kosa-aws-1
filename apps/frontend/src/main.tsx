import { StrictMode, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
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
type SourceReference = { source_id: string; source_version: string; locator: string; content_sha256: string };
type ExtractedRequirement = {
  source_locators: string[];
  requirement: string;
  requirement_summary: string;
  classification: string;
  mapping_reason: string;
  mapped_control_key: string | null;
  resource_types: string[];
  evaluation_type: string | null;
  applicability_semantics: string | null;
  required_evidence: string[];
  optional_evidence: string[];
  evaluation_rubric: string | null;
  severity_guidance: string | null;
  exception_semantics: string | null;
  compensating_control_semantics: string | null;
};
type Candidate = {
  rule_id: string;
  rule_version: string;
  classification: string;
  requirement: string;
  requirement_summary: string;
  mapping_reason: string;
  control_key: string;
  evaluation_type: string;
  proposed_severity: string;
  locators: SourceReference[];
  resource_types: string[];
  required_evidence: string[];
  optional_evidence: string[];
  applicability_semantics: string | null;
  evaluation_rubric: string | null;
  severity_guidance: string | null;
  exception_semantics: string | null;
  compensating_control_semantics: string | null;
};
type RejectedRequirement = { requirement: ExtractedRequirement; rejection_codes: string[] };
type CandidatePage = {
  status: string;
  counts: Record<string, number> | null;
  provenance: Record<string, unknown> | null;
  candidates: Candidate[];
  unsupported: ExtractedRequirement[];
  rejected: RejectedRequirement[];
  cursor: string | null;
};
type ResultRow = { resource_id: string; rule_id: string; rule_version: string; perspective: string; status: string; severity: string; score: number; rationale: string; evidence_references: string[]; model_profile_id: string; rubric_version: string };
type FindingRow = ResultRow & { finding_id: string };
type Suppression = { finding_id: string; exception_id: string; reason: string; expires_at: string };
type PublishedProfile = { policy_profile_id: string; version: string; rule_count: number; source_kinds: string[]; published_at?: string | null };
type SegmentReadiness = { kind: string; score: { score: number; evaluated_evaluations: number } | null };
type Report = { assessment_id: string; results: ResultRow[]; findings: FindingRow[]; readiness_score: { score: number; evaluated_evaluations: number } | null; segment_readiness?: SegmentReadiness[]; coverage: { percentage: number; completed_evaluations: number; planned_evaluations: number }; next_cursor?: string | null; findings_next_cursor?: string | null; suppressions?: Suppression[] };
type RemediationDecision = { action: string; manual_review_code: string | null; exception_id: string | null };
type RemediationStart = { decision: RemediationDecision; job: { job_id: string; remediation_id: string | null } | null };
type RemediationView = { remediation_id: string; status: string; decision: RemediationDecision; job_id: string | null; result: { kind: string; patch?: { changed_paths: string[]; base_commit_sha: string; artifact: { content_sha256: string } }; sync_target?: { commit_sha: string } } | null; pull_request: { number: number; url: string; head_branch: string } | null };

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
  // prompt=login forces the Hosted UI to re-authenticate every time instead of silently reusing
  // its session cookie. Without it, clicking "일반 사용자로 로그인" after an admin session just
  // lands back in the same account, so a role switch is impossible without a full logout.
  const q = new URLSearchParams({ client_id: COGNITO_CLIENT_ID, response_type: "code", scope: "openid email profile", redirect_uri: REDIRECT_URI, state, prompt: "login", code_challenge_method: "S256", code_challenge: await sha256(verifier) });
  window.location.assign(`https://${COGNITO_DOMAIN}/oauth2/authorize?${q}`);
}
/* Hosted UI keeps its own session cookie. Without ending it, "log in as someone else" silently
 * re-issues tokens for whoever signed in last — an admin who has just created a user cannot test
 * that user's login from the same browser. logout_uri must be in the pool client's LogoutURLs. */
function endCognitoSession() {
  sessionStorage.removeItem(verifierKey); sessionStorage.removeItem(stateKey);
  const q = new URLSearchParams({ client_id: COGNITO_CLIENT_ID, logout_uri: REDIRECT_URI });
  window.location.assign(`https://${COGNITO_DOMAIN}/logout?${q}`);
}
/* Mirrors the pool's password policy (Cognito default: 8+, upper, lower, number, symbol) so the
 * form can say what is missing before the request leaves the browser. The backend re-checks. */
function passwordProblems(pw: string): string[] {
  const out: string[] = [];
  if (pw.length < 8) out.push("8자 이상");
  if (!/[A-Z]/.test(pw)) out.push("대문자");
  if (!/[a-z]/.test(pw)) out.push("소문자");
  if (!/[0-9]/.test(pw)) out.push("숫자");
  if (!/[\^$*.\[\]{}()?"!@#%&/\\,><':;|_~`=+\-]/.test(pw)) out.push("기호");
  return out;
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

const AUTHORING_PENDING = new Set(["QUEUED", "PROCESSING"]);

/** `PolicySourceKind` 값의 한국어 표시. 모르는 값은 그대로 보여준다. */
const SEGMENT_LABELS: Record<string, string> = { INTERNAL_POLICY: "사내 정책", ISMS_P: "ISMS-P" };

/** 화면에 보이는 점수·비율은 정수로 반올림한다.
 *
 * 계약이 나르는 값은 그대로다 — `ReadinessScore.score`는 소수 둘째 자리까지의 연속 점수이고
 * API 응답에도 그 값이 실린다. 반올림은 표시에서만 한다: 보고서를 읽는 사람에게 `16.67`과
 * `17`은 같은 뜻이고, 소수점은 그 숫자가 실제보다 정밀하다는 인상을 준다. */
const rounded = (value: number) => Math.round(value);

async function fetchCandidatePage(token: string, sourceId: string, sourceVersion: string, cursor?: string): Promise<CandidatePage> {
  const query = new URLSearchParams({ limit: "50" });
  if (cursor) query.set("cursor", cursor);
  return api<CandidatePage>(`/policy-sources/${enc(sourceId)}/versions/${enc(sourceVersion)}/candidates?${query}`, token);
}

/** Read a completed immutable authoring run to the end instead of silently dropping page 2+. */
async function fetchAllCandidateResults(token: string, sourceId: string, sourceVersion: string): Promise<CandidatePage> {
  let first: CandidatePage | null = null;
  const candidates: Candidate[] = [];
  const unsupported: ExtractedRequirement[] = [];
  const rejected: RejectedRequirement[] = [];
  const seenCursors = new Set<string>();
  let cursor: string | undefined;

  for (;;) {
    const page = await fetchCandidatePage(token, sourceId, sourceVersion, cursor);
    if (page.status !== "READY") return page;
    first ??= page;
    candidates.push(...page.candidates);
    unsupported.push(...page.unsupported);
    rejected.push(...page.rejected);
    if (!page.cursor) break;
    if (seenCursors.has(page.cursor)) throw new Error("후보 조회 cursor가 반복되었습니다.");
    seenCursors.add(page.cursor);
    cursor = page.cursor;
  }

  if (!first) throw new Error("후보 조회 결과가 비어 있습니다.");
  return { ...first, candidates, unsupported, rejected, cursor: null };
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
    <p className="hint" style={{ marginTop: 14 }}>
      방금 만든 사용자로 들어가려면 먼저 <button className="linklike" onClick={() => endCognitoSession()}>현재 Cognito 세션을 끝내세요</button>. 끝내지 않으면 직전 계정으로 다시 로그인됩니다.
    </p>
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
      const d = await api<OrchestrationDecision>("/orchestrate", session.accessToken, { method: "POST", body: JSON.stringify(profileId ? { message: text, policy_profile_id: profileId } : { message: text }) });
      obs.lightNode("parent", "done");
      const sub: Record<string, GraphNodeId> = { POLICY_QA: "policy_qa", ASSESSMENT: "assessment", REMEDIATION: "remediation", DEPLOYMENT: "deployment" };
      const node = sub[d.intent];
      if (node) obs.lightNode(node, d.intent === "POLICY_QA" ? "done" : "pending");
      setTurns(t => [...t, { role: "bot", text: d.answer ?? d.rationale, decision: d }]);
    } catch (e) { obs.lightNode("parent", "failed"); setTurns(t => [...t, { role: "bot", text: `오류: ${(e as Error).message}` }]); }
    finally { setBusy(false); }
  }
  async function confirmAssessment(sel: Record<string, unknown> & { repository_id?: string }) {
    // The Parent extracts repository_id from free text ("test 리포지토리"), so it is a guess that
    // rarely equals the registered id (e.g. "test-s3-sandbox"). The connected repositories from
    // /scope are the source of truth: use the model's value only if it exactly matches one, else
    // fall back to the single connected repo. Sending the raw guess is what earns a 403.
    const connected = obs.obs.repos.map(r => r.repository_id);
    const proposed = sel.repository_id;
    const repositoryId =
      proposed && connected.includes(proposed) ? proposed
      : connected.length === 1 ? connected[0]
      : proposed;
    if (!repositoryId) {
      setTurns(t => [...t, { role: "bot", text: "평가할 리포지토리를 확인할 수 없습니다. 연결된 고객사 리소스가 없는지 확인하세요." }]);
      return;
    }
    if (connected.length > 1 && !connected.includes(repositoryId)) {
      setTurns(t => [...t, { role: "bot", text: `연결된 리포지토리(${connected.join(", ")}) 중 하나를 지정해 다시 요청해 주세요.` }]);
      return;
    }
    const policyProfileId = profileId ?? (sel.policy_profile_id as string | undefined);
    if (!policyProfileId) {
      setTurns(t => [...t, { role: "bot", text: "지정된 정책 Profile이 없습니다. 관리자에게 Profile 지정을 요청하세요." }]);
      return;
    }
    obs.lightNode("assessment", "active");
    obs.upsertJob({ id: "assess-" + Date.now(), label: "Assessment 시작", queue: "assessment", state: "active" });
    try {
      const r = await api<{ assessment_id?: string }>("/assessments", session.accessToken, { method: "POST", body: JSON.stringify({ repository_id: repositoryId, policy_profile_id: policyProfileId }) });
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
          {t.decision?.intent === "ASSESSMENT" && t.decision.selector && (() => {
            const connected = obs.obs.repos.map(r => r.repository_id);
            const proposed = t.decision.selector.repository_id;
            const repoShown =
              proposed && connected.includes(proposed) ? proposed
              : connected.length === 1 ? connected[0]
              : proposed ?? "?";
            return <div className="confirm">
              <div>제안 범위: repository <code>{repoShown}</code> · profile <code>{profileId ?? t.decision.selector.policy_profile_id ?? "미지정"}</code></div>
              <button style={{ marginTop: 8 }} onClick={() => void confirmAssessment(t.decision!.selector!)}>이 Assessment 시작</button>
            </div>;
          })()}
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

function CandidateField({ label, children, wide = false }: { label: string; children: ReactNode; wide?: boolean }) {
  return <div className={wide ? "candidate-field wide" : "candidate-field"}><dt>{label}</dt><dd>{children}</dd></div>;
}

function TextOrDash({ value }: { value: string | null }) {
  return <>{value?.trim() ? value : "-"}</>;
}

function CodeValues({ values }: { values: string[] }) {
  return values.length > 0
    ? <ul className="candidate-values">{values.map(value => <li key={value}><code>{value}</code></li>)}</ul>
    : <>-</>;
}

function CandidateCard({ candidate, checked, onToggle }: { candidate: Candidate; checked: boolean; onToggle: () => void }) {
  return <article className="candidate">
    <div className="candidate-heading">
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        aria-label={`${candidate.requirement_summary} 후보 선택`}
      />
      <div className="candidate-heading-main">
        <div className="candidate-identity">
          <span>Rule ID</span><code>{candidate.rule_id}</code>
          <span>Version</span><code>{candidate.rule_version}</code>
        </div>
        <div className="candidate-badges" aria-label="후보 분류">
          <span className="badge">{candidate.classification}</span>
          <span className="badge severity">{candidate.proposed_severity}</span>
          <span className="badge">{candidate.evaluation_type}</span>
        </div>
        <h3>{candidate.requirement_summary}</h3>
      </div>
    </div>
    <dl className="candidate-fields compact">
      <CandidateField label="Control" wide><code>{candidate.control_key}</code></CandidateField>
      <CandidateField label="요구사항" wide>{candidate.requirement}</CandidateField>
      <CandidateField label="매핑 근거" wide>{candidate.mapping_reason}</CandidateField>
    </dl>
    <details className="candidate-details">
      <summary>평가 형식과 근거 상세 보기</summary>
      <dl className="candidate-fields">
        <CandidateField label="Resource types"><CodeValues values={candidate.resource_types} /></CandidateField>
        <CandidateField label="Required evidence"><CodeValues values={candidate.required_evidence} /></CandidateField>
        <CandidateField label="Optional evidence"><CodeValues values={candidate.optional_evidence} /></CandidateField>
        <CandidateField label="평가 기준" wide><TextOrDash value={candidate.evaluation_rubric} /></CandidateField>
        <CandidateField label="적용 조건" wide><TextOrDash value={candidate.applicability_semantics} /></CandidateField>
        <CandidateField label="심각도 근거" wide><TextOrDash value={candidate.severity_guidance} /></CandidateField>
        <CandidateField label="예외 조건" wide><TextOrDash value={candidate.exception_semantics} /></CandidateField>
        <CandidateField label="보완 통제" wide><TextOrDash value={candidate.compensating_control_semantics} /></CandidateField>
        <CandidateField label="정책 근거" wide>
          {candidate.locators.length > 0 ? <ul className="candidate-references">{candidate.locators.map(reference => <li key={`${reference.source_id}:${reference.source_version}:${reference.locator}`}>
            <code>{reference.source_id}@{reference.source_version}#{reference.locator}</code>
            <span>SHA-256 {reference.content_sha256}</span>
          </li>)}</ul> : "-"}
        </CandidateField>
      </dl>
    </details>
  </article>;
}

function NonApprovableCard({ kind, requirement, rejectionCodes = [] }: { kind: "UNSUPPORTED" | "REJECTED"; requirement: ExtractedRequirement; rejectionCodes?: string[] }) {
  return <article className={`candidate non-approvable ${kind.toLowerCase()}`}>
    <div className="candidate-heading-main">
      <div className="candidate-badges"><span className="badge">{kind}</span>{rejectionCodes.map(code => <span className="badge" key={code}>{code}</span>)}</div>
      <h3>{requirement.requirement_summary}</h3>
    </div>
    <dl className="candidate-fields compact">
      <CandidateField label="요구사항" wide>{requirement.requirement}</CandidateField>
      <CandidateField label="분류 근거" wide>{requirement.mapping_reason}</CandidateField>
      <CandidateField label="원문 위치" wide><CodeValues values={requirement.source_locators} /></CandidateField>
    </dl>
  </article>;
}

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
  const [published, setPublished] = useState<PublishedProfile[]>([]);
  const [baselineId, setBaselineId] = useState("");
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
  const refreshProfiles = async () => {
    // 기준선을 고르려면 무엇이 게시돼 있는지 보여줘야 한다. 이름을 손으로 적게 하면 오타
    // 하나가 "기준선 없음"으로 조용히 게시된다.
    try { const r = await api<{ profiles: PublishedProfile[] }>("/policy-profiles", session.accessToken); setPublished(r.profiles); }
    catch { /* 기준선은 선택 사항이므로 목록을 못 읽어도 문서 화면은 유지 */ }
  };
  useEffect(() => { void refresh(); void refreshScope(); void refreshProfiles(); /* eslint-disable-next-line */ }, []);

  async function deleteDoc(d: Doc) {
    setError(null); setNotice(null);
    if (!confirm(`문서 '${d.filename}' (${d.status})를 삭제할까요? S3 원본·정규화 아티팩트와 DynamoDB 기록이 영구 삭제됩니다.`)) return;
    const jobId = "del-" + d.source_id;
    obs.upsertJob({ id: jobId, label: `삭제 ${d.filename}`, queue: "policy-sources", state: "active" });
    try {
      await api(`/policy-sources/${enc(d.source_id)}/versions/${enc(d.source_version)}`, session.accessToken, { method: "DELETE" });
      obs.upsertJob({ id: jobId, label: `삭제 ${d.filename}`, queue: "policy-sources", state: "done", meta: "완료" });
      setNotice(`삭제됨: ${d.filename} (원본·정규화 아티팩트, 추출 요청, 후보 항목까지 함께 삭제)`);
      // 지운 문서의 후보를 화면에 남겨두면 이미 없는 Rule을 장바구니에 담을 수 있다.
      setCandidates(c => { const next = { ...c }; delete next[d.source_id]; return next; });
      setCart(prev => prev.filter(i => i.source_id !== d.source_id));
      setOpenDoc(o => (o === d.source_id ? null : o));
      await refresh();
    } catch (e) {
      const msg = (e as Error).message;
      // 409는 "게시된 Profile이 아직 이 문서의 Rule을 인용한다"는 뜻이다. 아래 Profile 목록에서
      // 그 Profile을 retire하면 참조가 풀리고 문서를 지울 수 있다.
      const friendly = msg.includes("CONFLICT") ? "게시된 Profile이 이 문서의 Rule을 참조하고 있습니다. 아래 'Profile 목록'에서 해당 Profile을 삭제(retire)한 뒤 다시 시도하세요." : `삭제 실패: ${msg}`;
      obs.upsertJob({ id: jobId, label: `삭제 ${d.filename}`, queue: "policy-sources", state: "failed", meta: msg.includes("CONFLICT") ? "Profile 참조 중" : "실패" });
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
        try { p = await fetchCandidatePage(session.accessToken, s.source_id, s.source_version); }
        catch { obs.upsertJob({ id: "auth-" + s.source_id, label: "후보 추출", queue: "authoring", state: "active", meta: `대기 중 (${i + 1}/40)` }); continue; }
        obs.upsertJob({ id: "auth-" + s.source_id, label: "후보 추출", queue: "authoring", state: "active", meta: `${p.status} (${i + 1}/40)` });
        if (!AUTHORING_PENDING.has(p.status)) {
          result = p.status === "READY"
            ? await fetchAllCandidateResults(session.accessToken, s.source_id, s.source_version)
            : p;
          break;
        }
      }
      if (!result) throw new Error("후보 추출이 2분 내에 완료되지 않았습니다. 잠시 후 문서 목록에서 '후보 조회'를 누르세요.");
      if (result.status === "FAILED") throw new Error("후보 추출에 실패했습니다. 문서 상태와 Authoring Worker 로그를 확인하세요.");
      obs.patchPipeline("poll", "done"); obs.lightNode("authoring", "done");
      obs.upsertJob({ id: "auth-" + s.source_id, label: "후보 추출", queue: "authoring", state: "done", meta: `${result.candidates.length} 후보` });
      setCandidates(c => ({ ...c, [s.source_id]: result! }));
      setOpenDoc(s.source_id);
      setNotice(`추출 완료 · 승인 가능 ${result.candidates.length}개 · 미지원 ${result.unsupported.length}개 · 거절 ${result.rejected.length}개.`);
      await refresh();
    } catch (e) { setError((e as Error).message); obs.lightNode("authoring", "failed"); }
    finally { setRunning(false); }
  }

  async function loadCandidates(d: Doc) {
    setError(null); setOpenDoc(d.source_id);
    try {
      const p = await fetchAllCandidateResults(session.accessToken, d.source_id, d.source_version);
      setCandidates(c => ({ ...c, [d.source_id]: p }));
    } catch (e) {
      const msg = (e as Error).message;
      // 404는 "이 판본에 대한 추출 실행이 아직 없다"는 뜻이다. 장애가 아니므로 다시 눌러도
      // 달라지지 않는다 — 눌러야 할 버튼은 '추출 요청'이다.
      setError(msg.startsWith("404")
        ? "이 문서에는 아직 후보 추출 실행이 없습니다. '추출 요청'을 먼저 누르세요."
        : `후보 조회 실패: ${msg}`);
      setCandidates(c => { const next = { ...c }; delete next[d.source_id]; return next; });
    }
  }

  async function requestExtraction(d: Doc) {
    setError(null); setNotice(null);
    const jobId = "auth-" + d.source_id;
    obs.upsertJob({ id: jobId, label: "후보 추출", queue: "authoring", state: "active" });
    try {
      await api(`/policy-sources/${enc(d.source_id)}/versions/${enc(d.source_version)}/candidates`, session.accessToken, { method: "POST", body: "{}" });
      setNotice(`추출을 요청했습니다: ${d.filename}. 완료되면 '후보 조회'에 결과가 나옵니다.`);
      await loadCandidates(d);
    } catch (e) {
      const msg = (e as Error).message;
      obs.upsertJob({ id: jobId, label: "후보 추출", queue: "authoring", state: "failed", meta: "실패" });
      setError(`추출 요청 실패: ${msg}`);
    }
  }

  const inCart = (rid: string, rv: string) => cart.some(i => i.rule_id === rid && i.rule_version === rv);
  function toggleCart(sourceId: string, c: Candidate) {
    setCart(prev => inCart(c.rule_id, c.rule_version)
      ? prev.filter(i => !(i.rule_id === c.rule_id && i.rule_version === c.rule_version))
      : [...prev, { rule_id: c.rule_id, rule_version: c.rule_version, source_id: sourceId, source_version: c.rule_version, severity: c.proposed_severity, control_key: c.control_key }]);
  }

  async function publishCart() {
    const baseline = published.find(p => p.policy_profile_id === baselineId);
    if (cart.length === 0 && !baseline) { setError("장바구니에 후보를 담거나 기준선을 고르세요."); return; }
    if (!profileId.trim()) { setError("Profile ID를 입력하세요."); return; }
    if (baseline && baseline.policy_profile_id === profileId.trim()) { setError("기준선과 새 Profile ID가 같습니다. 다른 ID를 쓰세요."); return; }
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
      // 한 Profile은 여러 사내 문서의 승인 Rule과 ISMS-P 같은 운영자 기준선을 함께 담는다.
      // 승인된 Rule 전체가 문서별로 들어가며, 기준선은 이미 게시된 Profile에서 가져온다.
      // 승인은 판본마다 더해지므로 한 문서의 승인 집합은 커지기만 한다. 장바구니에 담긴 Rule만
      // 이 Profile에 넣으라고 명시해야, 화면이 보여준 것과 같은 Profile이 게시된다.
      const body: Record<string, unknown> = {
        policy_profile_id: profileId.trim(),
        version: "v1",
        sources: [...bySource.entries()].map(([source_id, e]) => ({ source_id, source_version: e.source_version })),
        rules: cart.map(i => ({ rule_id: i.rule_id, version: i.rule_version })),
      };
      if (baseline) body.baseline = { policy_profile_id: baseline.policy_profile_id, version: baseline.version };
      await api("/policy-profiles", session.accessToken, { method: "POST", body: JSON.stringify(body) });
      addProfile(profileId.trim());
      const parts = [
        bySource.size > 0 ? `사내 문서 ${bySource.size}건의 승인 Rule` : null,
        baseline ? `기준선 ${baseline.policy_profile_id}@${baseline.version} (Rule ${baseline.rule_count}개)` : null,
      ].filter(Boolean);
      setNotice(`Profile '${profileId.trim()}' 게시 완료 — ${parts.join(" + ")}. 준비도는 정책 원본별로 따로 표시됩니다. '사용자 관리'에서 지정하세요.`);
      setCart([]);
      await refreshProfiles();
    } catch (e) {
      const msg = (e as Error).message;
      setError(msg.includes("409")
        ? `게시 실패: 같은 ID의 Profile이 이미 있습니다(${profileId.trim()}). 다른 ID를 쓰거나 아래 목록에서 기존 Profile을 삭제하세요.`
        : `게시 실패: ${msg}`);
    }
  }

  async function retireProfile(p: PublishedProfile) {
    setError(null); setNotice(null);
    if (!confirm(`Profile '${p.policy_profile_id}'를 삭제(retire)할까요? 목록과 사용자 지정에서 빠지고 새 평가에 쓸 수 없게 됩니다. 이미 실행된 평가가 고정한 판본 기록은 보고서를 위해 남습니다.`)) return;
    try {
      await api(`/policy-profiles/${enc(p.policy_profile_id)}`, session.accessToken, { method: "DELETE" });
      setNotice(`Profile 삭제됨: ${p.policy_profile_id}`);
      if (baselineId === p.policy_profile_id) setBaselineId("");
      await refreshProfiles();
    } catch (e) { setError(`Profile 삭제 실패: ${(e as Error).message}`); }
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
            <button className="ghost" onClick={() => void requestExtraction(d)}>추출 요청</button>
            <button className="ghost" style={{ borderColor: "var(--err)", color: "var(--err)" }} onClick={() => void deleteDoc(d)}>삭제</button>
          </td>
        </tr>)}
          {docs.length === 0 && <tr><td colSpan={5} className="obs-empty">업로드된 문서가 없습니다.</td></tr>}</tbody></table>
    </div>

    {openDoc && candidates[openDoc] && <div className="card candidate-results">
      <h2>정책 Rule 후보 조회 결과</h2>
      <p className="hint">백엔드의 CandidateReviewEntry 형식을 그대로 표시합니다. 심각도는 Catalog가 정한 읽기 전용 값이며, 승인 가능한 후보만 장바구니에 담을 수 있습니다.</p>
      {AUTHORING_PENDING.has(candidates[openDoc].status) && <p className="hint">
        추출이 아직 끝나지 않았습니다 (상태 {candidates[openDoc].status}). 완결되지 않은 실행의 부분 결과는
        보여주지 않습니다 — 잠시 후 '후보 조회'를 다시 누르세요.
      </p>}
      <div className="candidate-summary" aria-label="후보 추출 집계">
        <span><strong>{candidates[openDoc].status}</strong><small>상태</small></span>
        <span><strong>{candidates[openDoc].counts?.accepted ?? candidates[openDoc].candidates.filter(c => c.classification === "AUTOMATABLE").length}</strong><small>자동 평가</small></span>
        <span><strong>{candidates[openDoc].counts?.manual ?? candidates[openDoc].candidates.filter(c => c.classification === "MANUAL").length}</strong><small>수동 검토</small></span>
        <span><strong>{candidates[openDoc].counts?.unsupported ?? candidates[openDoc].unsupported.length}</strong><small>미지원</small></span>
        <span><strong>{candidates[openDoc].counts?.rejected ?? candidates[openDoc].rejected.length}</strong><small>거절</small></span>
      </div>

      <section className="candidate-section" aria-labelledby="approvable-candidates">
        <h3 id="approvable-candidates">승인 가능한 후보 ({candidates[openDoc].candidates.length})</h3>
        {candidates[openDoc].candidates.length === 0 && <p className="obs-empty">승인 가능한 후보가 없습니다.</p>}
        {candidates[openDoc].candidates.map(c => <CandidateCard
          key={`${c.rule_id}@${c.rule_version}`}
          candidate={c}
          checked={inCart(c.rule_id, c.rule_version)}
          onToggle={() => toggleCart(openDoc, c)}
        />)}
      </section>

      <section className="candidate-section" aria-labelledby="unsupported-candidates">
        <h3 id="unsupported-candidates">미지원 요구사항 ({candidates[openDoc].unsupported.length})</h3>
        {candidates[openDoc].unsupported.length === 0 && <p className="obs-empty">미지원으로 분류된 요구사항이 없습니다.</p>}
        {candidates[openDoc].unsupported.map((requirement, index) => <NonApprovableCard
          key={`${requirement.requirement_summary}:${index}`}
          kind="UNSUPPORTED"
          requirement={requirement}
        />)}
      </section>

      <section className="candidate-section" aria-labelledby="rejected-candidates">
        <h3 id="rejected-candidates">검증 거절 ({candidates[openDoc].rejected.length})</h3>
        {candidates[openDoc].rejected.length === 0 && <p className="obs-empty">검증에서 거절된 요구사항이 없습니다.</p>}
        {candidates[openDoc].rejected.map((entry, index) => <NonApprovableCard
          key={`${entry.requirement.requirement_summary}:${index}`}
          kind="REJECTED"
          requirement={entry.requirement}
          rejectionCodes={entry.rejection_codes}
        />)}
      </section>
    </div>}

    <div className="card">
      <h2>Profile 장바구니 ({cart.length})</h2>
      <p className="hint">한 Profile에 여러 사내 문서의 승인 Rule과 ISMS-P 기준선을 함께 담을 수 있습니다. 담긴 원본은 Profile에 기록되며, 평가 준비도는 원본별로 따로 계산됩니다 — 사내 기준 미달과 인증 기준 미달은 서로 다른 조치를 부르므로 하나의 점수로 합치지 않습니다.</p>
      {cart.length === 0 ? <p className="obs-empty">담긴 후보가 없습니다. 문서에서 후보를 조회해 담거나, 기준선만으로 게시할 수 있습니다.</p>
        : <table><thead><tr><th>Rule</th><th>Severity</th><th>Control</th><th>문서</th></tr></thead>
          <tbody>{cart.map(i => <tr key={`${i.rule_id}@${i.rule_version}`}><td>{i.rule_id}@{i.rule_version}</td><td>{i.severity}</td><td>{i.control_key}</td><td>{i.source_id.slice(0, 12)}</td></tr>)}</tbody></table>}
      <div className="row" style={{ marginTop: 12 }}>
        <label style={{ flex: 1 }}>Policy Profile ID<input value={profileId} onChange={e => setProfileId(e.target.value)} placeholder="profile-internal-baseline" /></label>
        <label style={{ flex: 1 }}>기준선 (ISMS-P 등, 선택)
          <select value={baselineId} onChange={e => setBaselineId(e.target.value)}>
            <option value="">포함하지 않음</option>
            {published.filter(p => p.policy_profile_id !== profileId.trim()).map(p =>
              <option key={p.policy_profile_id} value={p.policy_profile_id}>
                {p.policy_profile_id}@{p.version} · Rule {p.rule_count}개{p.source_kinds.length ? ` · ${p.source_kinds.join("+")}` : ""}
              </option>)}
          </select>
        </label>
        <button disabled={cart.length === 0 && !baselineId} onClick={() => void publishCart()}>Profile 게시</button>
      </div>
      {published.length === 0 && <p className="hint">게시된 기준선 Profile이 없습니다. 운영자 bootstrap이 Registry를 이 고객 파티션에 게시하면 여기에 나타납니다.</p>}
    </div>

    <div className="card">
      <h2>Profile 목록 ({published.length})</h2>
      <p className="hint">삭제는 retire입니다 — 목록과 사용자 지정에서 빠지고 새 평가에 쓸 수 없게 됩니다. 이미 실행된 평가가 고정한 판본 기록은 보고서를 위해 남습니다. 문서를 지우려면 그 문서를 참조하는 Profile을 먼저 여기서 삭제하세요.</p>
      {published.length === 0 ? <p className="obs-empty">게시된 Profile이 없습니다.</p>
        : <table><thead><tr><th>Profile</th><th>버전</th><th>Rule</th><th>원본</th><th>게시</th><th></th></tr></thead>
          <tbody>{published.map(p => <tr key={p.policy_profile_id}>
            <td><code>{p.policy_profile_id}</code></td><td>{p.version}</td><td>{p.rule_count}</td>
            <td>{p.source_kinds.length ? p.source_kinds.map(k => SEGMENT_LABELS[k] ?? k).join(" + ") : "구분 없음"}</td>
            <td>{p.published_at ? String(p.published_at).slice(0, 16) : "-"}</td>
            <td><button className="ghost" style={{ borderColor: "var(--err)", color: "var(--err)" }} onClick={() => void retireProfile(p)}>삭제</button></td>
          </tr>)}</tbody></table>}
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
    const addr = email.trim().toLowerCase();
    if (!addr.includes("@")) { setError("이메일을 입력하세요."); return; }
    const problems = passwordProblems(pw);
    if (problems.length) { setError(`임시 비밀번호에 필요: ${problems.join(", ")}`); return; }
    try {
      await api("/admin/users", session.accessToken, { method: "POST", body: JSON.stringify({ email: addr, role, temporary_password: pw }) });
      setNotice(`사용자 생성: ${addr} (${role}) — 이 비밀번호를 그대로 전달하세요. 다른 브라우저(또는 세션 종료 후)에서 로그인합니다.`); setEmail(""); setPw("");
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
  async function deleteUser(u: User) {
    setError(null); setNotice(null);
    if (u.email === session.email) { setError("현재 로그인한 사용자는 삭제할 수 없습니다."); return; }
    if (!confirm(`사용자 '${u.email}'을(를) 삭제할까요? Cognito 계정과 그룹 소속이 영구 삭제됩니다.`)) return;
    try {
      await api("/admin/users", session.accessToken, { method: "DELETE", body: JSON.stringify({ email: u.email }) });
      setNotice(`삭제됨: ${u.email}`);
      await refresh();
    } catch (e) { setError(`삭제 실패: ${(e as Error).message}`); }
  }

  return <div className="panel">
    <div className="card">
      <h2>사용자 등록</h2>
      <p className="hint">고객(kosa-sandbox) 스코프의 사용자를 생성합니다. 이메일은 소문자로 저장됩니다. 임시 비밀번호는 8자 이상 + 대문자·소문자·숫자·기호 각 1개 이상 — 이 값이 그대로 로그인 비밀번호가 됩니다.</p>
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
      <table><thead><tr><th>이메일</th><th>역할/상태</th><th>지정 Profile</th><th></th></tr></thead>
        <tbody>{users.map(u => <tr key={u.username}><td>{u.email}</td><td>{u.status}{u.enabled ? "" : " (비활성)"}</td><td>{u.profile ? <code>{u.profile}</code> : "-"}</td>
          <td><button className="ghost" style={{ borderColor: "var(--err)", color: "var(--err)" }} disabled={u.email === session.email} onClick={() => void deleteUser(u)}>삭제</button></td></tr>)}
          {users.length === 0 && <tr><td colSpan={4} className="obs-empty">사용자가 없습니다.</td></tr>}</tbody></table>
      <p className="hint">Profile 목록은 이 브라우저에서 게시한 것을 보여줍니다(백엔드 list-profiles 미구현).</p>
    </div>
    {notice && <p className="status">{notice}</p>}
    {error && <p className="alert">{error}</p>}
  </div>;
}

/* =========================================================================
 * Assessment report — polls to completion, shows evidence, requests remediation
 * =======================================================================*/
const REPORT_PAGE = 100;
const FOLLOW_UP = new Set(["FAIL", "MANUAL_REVIEW", "INSUFFICIENT_EVIDENCE"]);

/** Read the whole immutable report: follow both opaque cursors (results, findings). */
async function fetchFullReport(token: string, assessmentId: string): Promise<Report> {
  const first = await api<Report>(`/assessments/${enc(assessmentId)}?limit=${REPORT_PAGE}`, token);
  const results = [...first.results];
  const findings = [...first.findings];
  const suppressions = [...(first.suppressions ?? [])];
  let cursor = first.next_cursor ?? null;
  let findingsCursor = first.findings_next_cursor ?? null;
  for (let guard = 0; (cursor || findingsCursor) && guard < 50; guard++) {
    const q = new URLSearchParams({ limit: String(REPORT_PAGE) });
    if (cursor) q.set("cursor", cursor);
    if (findingsCursor) q.set("findings_cursor", findingsCursor);
    const page = await api<Report>(`/assessments/${enc(assessmentId)}?${q}`, token);
    if (cursor) results.push(...page.results);
    if (findingsCursor) { findings.push(...page.findings); suppressions.push(...(page.suppressions ?? [])); }
    cursor = cursor ? (page.next_cursor ?? null) : null;
    findingsCursor = findingsCursor ? (page.findings_next_cursor ?? null) : null;
  }
  return { ...first, results, findings, suppressions, next_cursor: null, findings_next_cursor: null };
}

function isComplete(rep: Report) { return rep.coverage.completed_evaluations >= rep.coverage.planned_evaluations; }

function ReportPanel({ session, assessmentId }: { session: Session; assessmentId: string }) {
  const [rep, setRep] = useState<Report | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    // POST /assessments only queues the job (202); the worker writes the plan first and then one
    // immutable result per Resource × Rule × Perspective. Poll through the initial 404 window and
    // then keep refreshing until coverage reaches the planned denominator — the Job record is not
    // advanced by the worker, so coverage is the only completion signal the API exposes.
    let cancelled = false;
    setRep(null); setErr(null); setWaiting(true); setAttempt(0);
    (async () => {
      for (let i = 0; i < 200 && !cancelled; i++) {
        setAttempt(i + 1);
        try {
          const r = await fetchFullReport(session.accessToken, assessmentId);
          if (cancelled) return;
          setRep(r);
          if (isComplete(r)) { setWaiting(false); return; }
        } catch (e) {
          const msg = (e as Error).message;
          // Keep polling only while the report is not yet created; surface anything else.
          if (!msg.includes("404") && !msg.includes("NOT_FOUND")) {
            if (!cancelled) { setErr(msg); setWaiting(false); }
            return;
          }
        }
        await sleep(3000);
      }
      if (!cancelled) { setWaiting(false); if (!rep) setErr("평가 결과가 아직 준비되지 않았습니다. 잠시 후 다시 열어주세요."); }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line
  }, [assessmentId, session.accessToken]);

  if (err && !rep) return <div className="panel"><div className="card"><p className="alert">{err}</p></div></div>;
  if (!rep) return <div className="panel"><div className="card"><p className="obs-empty">{waiting ? `평가 실행 중… 결과를 기다리는 중입니다 (조회 ${attempt}회).` : "Assessment 결과를 불러오는 중…"}</p></div></div>;

  const complete = isComplete(rep);
  const countStatus = (s: string) => rep.results.filter(r => r.status === s).length;
  const severe = rep.findings.filter(f => f.status === "FAIL" && (f.severity === "CRITICAL" || f.severity === "HIGH")).length;
  const rules = new Set(rep.results.map(r => `${r.rule_id}@${r.rule_version}`));
  const resources = new Set(rep.results.map(r => r.resource_id));
  const modelProfiles = new Set(rep.results.map(r => `${r.model_profile_id} · rubric ${r.rubric_version}`));
  const suppressed = new Map((rep.suppressions ?? []).map(s => [s.finding_id, s]));
  const sorted = [...rep.results].sort((a, b) => a.resource_id.localeCompare(b.resource_id) || a.rule_id.localeCompare(b.rule_id) || a.perspective.localeCompare(b.perspective));
  const segments = rep.segment_readiness ?? [];

  return <div className="panel">
    <div className="card"><h2>Assessment 결과 <code>{rep.assessment_id}</code></h2>
      <div className="row">
        <span>실행률 <strong>{rounded(rep.coverage.percentage)}%</strong> ({rep.coverage.completed_evaluations}/{rep.coverage.planned_evaluations}){!complete && <span className="hint"> — 평가 진행 중, 자동 갱신 (조회 {attempt}회)</span>}</span>
        {segments.length === 0 && <span>Readiness Score <strong>{rep.readiness_score ? rounded(rep.readiness_score.score) : (complete ? "계산 불가" : "계산 대기")}</strong>{rep.readiness_score && <span className="hint"> / 100 · 평가 {rep.readiness_score.evaluated_evaluations}건 가중 평균</span>}</span>}
      </div>
      {/* Profile이 여러 원본에 걸치면 점수를 원본별로만 보여준다. 두 준비도를 합친 하나의
          숫자는 어느 기준에 대한 답도 아니며, 한쪽의 미달을 다른 쪽이 가린다. */}
      {segments.length > 0 && <div className="candidate-summary" aria-label="정책 원본별 준비도">
        {segments.map(s => <span key={s.kind}>
          <strong>{s.score ? rounded(s.score.score) : (complete ? "계산 불가" : "계산 대기")}</strong>
          <small>{SEGMENT_LABELS[s.kind] ?? s.kind} 준비도{s.score ? ` · 평가 ${s.score.evaluated_evaluations}건` : ""}</small>
        </span>)}
      </div>}
      <p className="disclaimer">Readiness Score는 0–100 연속 점수의 severity 가중 평균으로, 선택한 Policy Profile에 대한 <strong>준비도</strong> 지표입니다. 공식 ISMS-P 인증 점수나 합격/불합격 판정이 아닙니다. 한 Profile이 사내 정책과 ISMS-P를 함께 담으면 준비도는 <strong>원본별로 따로</strong> 계산해 표시하며 하나의 점수로 합치지 않습니다 — 한 Rule이 두 기준을 함께 뒷받침하면 그 Rule은 양쪽 점수에 들어갑니다. DRIFT·MANUAL 관점과 OUT_OF_SCOPE·EXECUTION_ERROR는 점수에서 제외됩니다.</p>
      <div className="candidate-summary" aria-label="평가 집계">
        <span><strong>{resources.size}</strong><small>평가 리소스</small></span>
        <span><strong>{rules.size}</strong><small>평가 Rule</small></span>
        <span><strong>{severe}</strong><small>CRITICAL/HIGH FAIL</small></span>
        <span><strong>{countStatus("MANUAL_REVIEW")}</strong><small>수동 검토</small></span>
        <span><strong>{countStatus("INSUFFICIENT_EVIDENCE")}</strong><small>근거 부족</small></span>
      </div>
      {countStatus("EXECUTION_ERROR") > 0 && <p className="alert">EXECUTION_ERROR {countStatus("EXECUTION_ERROR")}건 — 평가가 실행되지 못한 좌표입니다. 비준수와 다르며 Coverage 분모에 남습니다.</p>}
      <p className="hint">Model Profile: {[...modelProfiles].join(", ") || "-"}</p>
    </div>

    <div className="card"><h2>Findings ({rep.findings.length})</h2>
      <p className="hint">FAIL·MANUAL_REVIEW·INSUFFICIENT_EVIDENCE 결과만 Finding이 됩니다. Evidence는 평가기가 인용한 정책 locator와 read-only IaC/AWS 조회 locator입니다.</p>
      {rep.findings.map(f => <FindingCard key={f.finding_id} finding={f} suppression={suppressed.get(f.finding_id)} session={session} />)}
      {rep.findings.length === 0 && <p className="obs-empty">{complete ? "Finding이 없습니다." : "아직 Finding이 없습니다 (평가 진행 중)."}</p>}
    </div>

    <div className="card"><h2>Resource × Rule × Perspective 결과 ({rep.results.length})</h2>
      <div style={{ overflowX: "auto" }}>
      <table><thead><tr><th>Resource</th><th>Rule</th><th>관점</th><th>상태</th><th>Score</th><th>Severity</th></tr></thead>
        <tbody>{sorted.map(r => <tr key={`${r.resource_id}|${r.rule_id}|${r.perspective}`} title={r.rationale}>
          <td><code>{r.resource_id}</code></td><td>{r.rule_id}@{r.rule_version}</td><td>{r.perspective}</td>
          <td><span className={`badge status-${r.status.toLowerCase()}`}>{r.status}</span></td>
          <td>{FOLLOW_UP.has(r.status) || r.status === "PASS" ? r.score : "-"}</td><td>{r.severity}</td>
        </tr>)}
        {sorted.length === 0 && <tr><td colSpan={6} className="obs-empty">결과가 아직 없습니다.</td></tr>}</tbody></table>
      </div>
    </div>
  </div>;
}

function FindingCard({ finding: f, suppression, session }: { finding: FindingRow; suppression?: Suppression; session: Session }) {
  const [start, setStart] = useState<RemediationStart | null>(null);
  const [view, setView] = useState<RemediationView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function request() {
    setBusy(true); setError(null); setStart(null); setView(null);
    try {
      const r = await api<RemediationStart>(`/findings/${enc(f.finding_id)}/remediations`, session.accessToken, { method: "POST", body: "{}" });
      setStart(r);
      const remediationId = r.job?.remediation_id;
      if (remediationId) {
        // The worker generates the patch and opens the PR asynchronously; read the stored record
        // until the result (and, for a patch, the pull request) lands.
        for (let i = 0; i < 60; i++) {
          await sleep(3000);
          try {
            const v = await api<RemediationView>(`/remediations/${enc(remediationId)}`, session.accessToken);
            setView(v);
            if (v.result && (v.result.kind !== "TERRAFORM_PATCH" || v.pull_request)) break;
          } catch (e) { const msg = (e as Error).message; if (!msg.includes("404")) throw e; }
        }
      }
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  const code = start?.decision.manual_review_code;
  return <article className="candidate">
    <div className="candidate-badges"><span className="badge severity">{f.severity}</span><span className={`badge status-${f.status.toLowerCase()}`}>{f.status}</span><span className="badge">{f.perspective}</span><span className="badge">score {f.score}</span>{suppression && <span className="badge">억제됨 · {suppression.reason}</span>}</div>
    <h3><code>{f.resource_id}</code> · {f.rule_id}@{f.rule_version}</h3>
    <p style={{ margin: "6px 0" }}>{f.rationale}</p>
    <dl className="candidate-fields"><div className="candidate-field wide"><dt>Evidence</dt><dd><CodeValues values={f.evidence_references} /></dd></div></dl>
    <div className="finding-actions row">
      <button className="ghost" disabled={busy || !!suppression} onClick={() => void request()}>{busy ? "조치 판정 중…" : "조치 요청"}</button>
      {start && <span className="hint">판정: <strong>{start.decision.action}</strong>{code ? ` (${code})` : ""}{start.job ? ` · job ${start.job.job_id}` : " · Job 없음(사람 검토)"}</span>}
    </div>
    {start && start.decision.action === "MANUAL_REVIEW" && <p className="hint">자동 조치 대상이 아닙니다{code === "RULE_NOT_IN_SCOPE" ? " — 이 Rule은 자동 patch 허용 범위(remediation eligibility)에 등록되어 있지 않습니다" : ""}. 담당자가 직접 검토합니다.</p>}
    {view && <div className="remediation-result">
      <div className="hint">remediation <code>{view.remediation_id}</code> · {view.status}{view.result ? ` · ${view.result.kind}` : " · Worker 결과 대기 중"}</div>
      {view.result?.patch && <div className="hint">변경 파일: <CodeValues values={view.result.patch.changed_paths} /> base commit <code>{view.result.patch.base_commit_sha.slice(0, 12)}</code> · patch digest <code>{view.result.patch.artifact.content_sha256.slice(0, 16)}</code></div>}
      {view.result?.sync_target && <div className="hint">IaC는 이미 안전합니다. 배포 대상 commit <code>{view.result.sync_target.commit_sha.slice(0, 12)}</code>로 Actual 동기화를 진행합니다.</div>}
      {view.pull_request
        ? <p className="status">Pull Request #{view.pull_request.number} 생성됨 — <a href={view.pull_request.url} target="_blank" rel="noreferrer">{view.pull_request.url}</a> (branch <code>{view.pull_request.head_branch}</code>). PR 본문에 unified diff가 있습니다. 사람이 검토·머지한 뒤에만 배포 승인과 apply가 진행됩니다.</p>
        : view.result?.kind === "TERRAFORM_PATCH" && <p className="hint">PR 생성 대기 중…</p>}
    </div>}
    {error && <p className="alert">{error}</p>}
  </article>;
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
    if (!session) return;
    // /scope is available to any authenticated caller and returns only their own customer's
    // connected repositories — the chatbot needs it to resolve an assessment target, so load it
    // for everyone. Only the admin roster (/admin/users) is admin-only.
    void (async () => {
      try { const r = await api<{ repositories: RepoScope[] }>("/scope", session.accessToken); observer.setRepos(r.repositories); } catch { /* keep panel */ }
    })();
    if (!isAdmin) {
      // A regular user cannot call the admin list; show only their own assignment from the token.
      observer.setUserProfiles([{ email: session.email, profile: session.profile }]);
      return;
    }
    void (async () => {
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
        <button className="logout-btn" title="Cognito 세션을 끝내고 로그인 화면으로" onClick={() => endCognitoSession()}>로그아웃</button>
      </div>
      {view === "chat" && <Chat session={session} obs={observer} profileId={myProfile} onAssessment={id => { setAssessmentId(id); setView("report"); }} />}
      {view === "documents" && isAdmin && <DocumentsPanel session={session} obs={observer} />}
      {view === "users" && isAdmin && <UsersPanel session={session} obs={observer} />}
      {view === "report" && assessmentId && <ReportPanel session={session} assessmentId={assessmentId} />}
    </div>
  </div>;
}
createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
