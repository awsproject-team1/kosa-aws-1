import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Result = { resource_id: string; rule_id: string; perspective: string; status: string; score: number; severity: string; rationale: string };
type Finding = { finding_id: string; resource_id: string; rule_id: string; perspective: string; status: string; severity: string; score: number; rationale: string };
type ReadinessScore = { score: number; evaluated_evaluations: number };
type Report = { assessment_id: string; results: Result[]; findings: Finding[]; readiness_score: ReadinessScore | null; next_cursor: string | null; findings_next_cursor: string | null; coverage: { planned_evaluations: number; completed_evaluations: number; percentage: number } };
type RemediationDecision = { finding_id: string; resource_id: string; rule_id: string; rule_version: string; perspective: string; action: string; manual_review_code: string | null; exception_id: string | null };
type RemediationStart = { decision: RemediationDecision; job: { job_id: string; status: string } | null };
type Deployment = { deployment_id: string; status: string; commit_sha: string; remediation_id: string; source_assessment_id: string; plan_hash: string | null; verification_assessment_id: string | null };
type Coverage = { planned_evaluations: number; completed_evaluations: number; percentage: number };
type FindingResolution = { resource_id: string; rule_id: string; rule_version: string; perspective: string; resolution: string };
type Comparison = { source_assessment_id: string; verification_assessment_id: string; deployment_id: string; comparable: boolean; ineligibility_reasons: string[]; source_coverage: Coverage; verification_coverage: Coverage; source_readiness_score: ReadinessScore | null; verification_readiness_score: ReadinessScore | null; readiness_score_delta: number | null; finding_resolutions: FindingResolution[] };

//: Enumerated reject reasons; the API rejects free text (ADR-0019 §8), so the UI offers the
//: exact enum rather than a text box.
const rejectionReasons = ["NOT_APPROVED_BY_POLICY", "PLAN_OUTDATED", "RISK_TOO_HIGH", "SUPERSEDED", "OTHER"] as const;

//: Statuses at which an Admin may still act. `WAITING_APPROVAL` is the only one that accepts an
//: approval; the derived status is presentation-only and the API re-checks facts either way
//: (ADR-0019 §8), so this only decides which controls to render.
const approvableStatuses = new Set(["WAITING_APPROVAL"]);
const rejectableStatuses = new Set(["PLAN_COMPLETED", "READINESS_EVALUATED", "WAITING_APPROVAL", "BLOCKED", "MANUAL_REVIEW"]);

const verifierKey = "governance.oauth.pkce.verifier";
const stateKey = "governance.oauth.state";
const returnToKey = "governance.oauth.return-to";
const cognitoDomain = import.meta.env.VITE_COGNITO_DOMAIN;
const cognitoClientId = import.meta.env.VITE_COGNITO_CLIENT_ID;
const redirectUri = import.meta.env.VITE_COGNITO_REDIRECT_URI ?? window.location.origin;

function base64Url(bytes: Uint8Array) {
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function sha256(value: string) {
  return base64Url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))));
}

async function startLogin() {
  if (!cognitoDomain || !cognitoClientId) throw new Error("Cognito frontend configuration is missing.");
  const verifier = base64Url(crypto.getRandomValues(new Uint8Array(32)));
  const state = base64Url(crypto.getRandomValues(new Uint8Array(16)));
  sessionStorage.setItem(verifierKey, verifier);
  sessionStorage.setItem(stateKey, state);
  const returnTo = new URL(window.location.href);
  returnTo.searchParams.delete("code");
  returnTo.searchParams.delete("state");
  sessionStorage.setItem(returnToKey, `${returnTo.pathname}${returnTo.search}${returnTo.hash}`);
  const query = new URLSearchParams({ client_id: cognitoClientId, response_type: "code", scope: "openid email", redirect_uri: redirectUri, state, code_challenge_method: "S256", code_challenge: await sha256(verifier) });
  window.location.assign(`https://${cognitoDomain}/oauth2/authorize?${query}`);
}

async function exchangeCallback() {
  const query = new URLSearchParams(window.location.search);
  const code = query.get("code");
  if (!code) return null;
  if (!cognitoDomain || !cognitoClientId) throw new Error("Cognito frontend configuration is missing.");
  if (query.get("state") !== sessionStorage.getItem(stateKey)) throw new Error("Cognito login state did not match.");
  const verifier = sessionStorage.getItem(verifierKey);
  if (!verifier) throw new Error("Cognito login verifier is missing.");
  const body = new URLSearchParams({ grant_type: "authorization_code", client_id: cognitoClientId, code, redirect_uri: redirectUri, code_verifier: verifier });
  const response = await fetch(`https://${cognitoDomain}/oauth2/token`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body });
  if (!response.ok) throw new Error("Cognito access token exchange failed.");
  const token = await response.json() as { access_token?: unknown };
  if (typeof token.access_token !== "string" || !token.access_token) throw new Error("Cognito did not return an access token.");
  sessionStorage.removeItem(verifierKey);
  sessionStorage.removeItem(stateKey);
  const returnTo = sessionStorage.getItem(returnToKey);
  sessionStorage.removeItem(returnToKey);
  const destination = returnTo?.startsWith("/") && !returnTo.startsWith("//")
    ? returnTo
    : window.location.pathname;
  history.replaceState({}, "", destination);
  return token.access_token;
}

function StartAssessment({ accessToken, onStarted }: { accessToken: string; onStarted: (assessmentId: string) => void }) {
  const [repositoryId, setRepositoryId] = useState("");
  const [policyProfileId, setPolicyProfileId] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const result = await api<{ assessment_id?: unknown }>("/assessments", accessToken, {
      method: "POST",
      body: JSON.stringify({ repository_id: repositoryId, policy_profile_id: policyProfileId }),
    });
    if (typeof result.assessment_id !== "string" || !result.assessment_id) throw new Error("Assessment ID를 받지 못했습니다.");
    onStarted(result.assessment_id);
  }
  return <main><h1>Initial Assessment</h1><form onSubmit={event => void submit(event).catch((reason: Error) => setError(reason.message))}><label>Repository ID <input required value={repositoryId} onChange={event => setRepositoryId(event.target.value)} /></label><label>Policy Profile ID <input required value={policyProfileId} onChange={event => setPolicyProfileId(event.target.value)} /></label><button type="submit">Assessment 시작</button>{error && <p role="alert">{error}</p>}</form></main>;
}

function AssessmentReport({ assessmentId, accessToken }: { assessmentId: string; accessToken: string }) {
  const [report, setReport] = useState<Report | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [findingsCursor, setFindingsCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    // Without an assessment_id there is nothing to read yet; the start form owns that step.
    if (!assessmentId) return;
    const params = new URLSearchParams({ limit: "25" });
    if (cursor) params.set("cursor", cursor);
    if (findingsCursor) params.set("findings_cursor", findingsCursor);
    api<Report>(`/assessments/${encodeURIComponent(assessmentId)}?${params}`, accessToken)
      .then(next => setReport(previous => previous && (cursor || findingsCursor) ? { ...next, results: uniqueResults([...previous.results, ...next.results]), findings: uniqueFindings([...previous.findings, ...next.findings]) } : next))
      .catch((reason: Error) => setError(reason.message));
  }, [accessToken, assessmentId, cursor, findingsCursor]);
  if (error) return <p role="alert">{error}</p>;
  if (!report) return <p>Assessment 결과를 불러오는 중…</p>;
  return <main><h1>Initial Assessment</h1><section><strong>평가 실행률 {report.coverage.percentage}%</strong><span>{report.coverage.completed_evaluations} / {report.coverage.planned_evaluations} applicable evaluations</span><strong>Readiness Score {report.readiness_score ? report.readiness_score.score : "계산 대기"}</strong></section><h2>Findings ({report.findings.length})</h2><table><thead><tr><th>Resource</th><th>Rule</th><th>Perspective</th><th>Status</th><th>Severity</th><th>Score</th><th>조치</th></tr></thead><tbody>{report.findings.map(finding => <tr key={finding.finding_id}><td>{finding.resource_id}</td><td>{finding.rule_id}</td><td>{finding.perspective}</td><td>{finding.status}</td><td>{finding.severity}</td><td>{finding.score}</td><td><RemediateFinding finding={finding} accessToken={accessToken} /></td></tr>)}</tbody></table>{report.findings_next_cursor && <button onClick={() => setFindingsCursor(report.findings_next_cursor)}>Findings 더 보기</button>}<h2>Evaluation results</h2><table><thead><tr><th>Resource</th><th>Rule</th><th>Perspective</th><th>Status</th><th>Score</th></tr></thead><tbody>{report.results.map(result => <tr key={`${result.resource_id}-${result.rule_id}-${result.perspective}`}><td>{result.resource_id}</td><td>{result.rule_id}</td><td>{result.perspective}</td><td>{result.status}</td><td>{result.score}</td></tr>)}</tbody></table>{report.next_cursor && <button onClick={() => setCursor(report.next_cursor)}>Load more</button>}</main>;
}

/**
 * Owns the customer session and picks the screen.
 *
 * The access token lives here rather than in each screen so the deployment approval screen and
 * the Assessment screen cannot end up authenticated differently.
 */
function routeFromLocation() {
  const params = new URLSearchParams(window.location.search);
  return {
    assessmentId: params.get("assessment_id") ?? "",
    deploymentId: params.get("deployment_id") ?? "",
  };
}

function App() {
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [route, setRoute] = useState(routeFromLocation);
  useEffect(() => {
    exchangeCallback()
      .then(token => {
        if (token) setAccessToken(token);
        setRoute(routeFromLocation());
      })
      .catch((reason: Error) => setError(reason.message));
    const onPopState = () => setRoute(routeFromLocation());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  if (!accessToken) {
    return <main>
      <h1>Cloud Governance</h1>
      <p>고객 Cognito 계정으로 로그인해 Assessment 결과와 배포 승인을 확인하세요.</p>
      <button onClick={() => void startLogin().catch((reason: Error) => setError(reason.message))}>Cognito로 로그인</button>
      {error && <p role="alert">{error}</p>}
    </main>;
  }
  if (route.deploymentId) return <DeploymentPanel deploymentId={route.deploymentId} accessToken={accessToken} />;
  if (!route.assessmentId) return <StartAssessment accessToken={accessToken} onStarted={assessmentId => {
    const params = new URLSearchParams(window.location.search);
    params.delete("deployment_id");
    params.set("assessment_id", assessmentId);
    history.pushState({}, "", `${window.location.pathname}?${params}${window.location.hash}`);
    setRoute(routeFromLocation());
  }} />;
  return <AssessmentReport assessmentId={route.assessmentId} accessToken={accessToken} />;
}

function uniqueResults(values: Result[]) { return [...new Map(values.map(value => [`${value.resource_id}:${value.rule_id}:${value.perspective}`, value])).values()]; }
function uniqueFindings(values: Finding[]) { return [...new Map(values.map(value => [value.finding_id, value])).values()]; }

/** Call the governance API with the customer's access token, surfacing the API error body. */
async function api<T>(path: string, accessToken: string, init?: RequestInit): Promise<T> {
  const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "";
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: { ...(init?.body ? { "content-type": "application/json" } : {}), Authorization: `Bearer ${accessToken}`, ...init?.headers },
  });
  if (!response.ok) {
    // The API returns an enumerated error code, never policy text or resource internals.
    const detail = await response.json().catch(() => null) as { code?: unknown } | null;
    throw new Error(typeof detail?.code === "string" ? `${response.status} ${detail.code}` : `요청이 실패했습니다 (${response.status}).`);
  }
  return response.json() as Promise<T>;
}

/**
 * Start remediation for one Finding.
 *
 * The response is a policy decision, not a promise of change: a non-actionable decision is a
 * normal 200 with no Job, so the UI reports the decision and its `manual_review_code` instead
 * of implying that something is being fixed.
 */
function RemediateFinding({ finding, accessToken }: { finding: Finding; accessToken: string }) {
  const [start, setStart] = useState<RemediationStart | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (start) {
    const { decision, job } = start;
    return <span>{decision.action}{decision.manual_review_code ? ` (${decision.manual_review_code})` : ""}{job ? ` · job ${job.job_id}` : ""}</span>;
  }
  return <>
    <button disabled={busy} onClick={() => {
      setBusy(true);
      api<RemediationStart>(`/findings/${encodeURIComponent(finding.finding_id)}/remediations`, accessToken, { method: "POST" })
        .then(setStart).catch((reason: Error) => setError(reason.message)).finally(() => setBusy(false));
    }}>조치 요청</button>
    {error && <span role="alert">{error}</span>}
  </>;
}

/**
 * Admin deployment screen: derived status, human approval gate, reject, and verification.
 *
 * Approval echoes the exact `commit_sha`/`plan_hash` the status read returned. That is
 * deliberate — the API refuses an approval whose pair does not match the stored plan
 * (ADR-0019 §4), so an operator approving a plan that was replaced meanwhile is rejected
 * rather than silently approving the newer one.
 */
function DeploymentPanel({ deploymentId, accessToken }: { deploymentId: string; accessToken: string }) {
  const [deployment, setDeployment] = useState<Deployment | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [reason, setReason] = useState<string>(rejectionReasons[0]);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    api<Deployment>(`/deployments/${encodeURIComponent(deploymentId)}`, accessToken)
      .then(setDeployment).catch((reason: Error) => setError(reason.message));
  }, [deploymentId, accessToken, reload]);

  useEffect(() => {
    // The verification Assessment exists only after apply completes, so this read is
    // attempted only when the deployment says it has one.
    if (!deployment?.verification_assessment_id) return;
    api<Comparison>(`/deployments/${encodeURIComponent(deploymentId)}/verification`, accessToken)
      .then(setComparison).catch((reason: Error) => setError(reason.message));
  }, [deployment?.verification_assessment_id, deploymentId, accessToken]);

  if (error) return <main><h1>Deployment</h1><p role="alert">{error}</p></main>;
  if (!deployment) return <main><h1>Deployment</h1><p>배포 상태를 불러오는 중…</p></main>;

  const canApprove = approvableStatuses.has(deployment.status) && deployment.plan_hash !== null;
  const canReject = rejectableStatuses.has(deployment.status);

  function act(path: string, body: object, done: string) {
    setNotice(null);
    setError(null);
    api<unknown>(path, accessToken, { method: "POST", body: JSON.stringify(body) })
      .then(() => { setNotice(done); setReload(value => value + 1); })
      .catch((reason: Error) => setError(reason.message));
  }

  return <main>
    <h1>Deployment</h1>
    <section>
      <strong>{deployment.status}</strong>
      <span>commit {deployment.commit_sha.slice(0, 12)}</span>
      <span>plan {deployment.plan_hash ? `${deployment.plan_hash.slice(0, 12)}…` : "미생성"}</span>
    </section>
    <p>Remediation {deployment.remediation_id} · 원 Assessment {deployment.source_assessment_id}</p>
    {notice && <p role="status">{notice}</p>}
    {!canApprove && !canReject && <p>이 상태에서는 사람이 승인하거나 거절할 수 없습니다.</p>}
    {canApprove && <button onClick={() => act(
      `/deployments/${encodeURIComponent(deploymentId)}/approve`,
      { commit_sha: deployment.commit_sha, plan_hash: deployment.plan_hash },
      "승인했습니다. apply는 Deployment Worker가 재검증 후 실행합니다.",
    )}>이 plan을 승인</button>}
    {canReject && <>
      <label>거절 사유 <select value={reason} onChange={event => setReason(event.target.value)}>
        {rejectionReasons.map(value => <option key={value} value={value}>{value}</option>)}
      </select></label>
      <button onClick={() => act(`/deployments/${encodeURIComponent(deploymentId)}/reject`, { reason }, "거절했습니다.")}>거절</button>
    </>}
    {comparison && <>
      <h2>Post-Deploy Verification</h2>
      {comparison.comparable
        ? <section>
            <strong>Readiness {comparison.source_readiness_score?.score} → {comparison.verification_readiness_score?.score}</strong>
            <span>변화 {comparison.readiness_score_delta}</span>
          </section>
        : <p role="alert">비교할 수 없습니다: {comparison.ineligibility_reasons.join(", ")}</p>}
      <table><thead><tr><th>Resource</th><th>Rule</th><th>Rule version</th><th>Perspective</th><th>Resolution</th></tr></thead><tbody>
        {comparison.finding_resolutions.map(value => <tr key={`${value.resource_id}:${value.rule_id}:${value.perspective}`}>
          <td>{value.resource_id}</td><td>{value.rule_id}</td><td>{value.rule_version}</td><td>{value.perspective}</td><td>{value.resolution}</td>
        </tr>)}
      </tbody></table>
    </>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
