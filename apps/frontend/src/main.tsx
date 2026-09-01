import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Result = { resource_id: string; rule_id: string; perspective: string; status: string; score: number; severity: string; rationale: string };
type Finding = { finding_id: string; resource_id: string; rule_id: string; perspective: string; status: string; severity: string; score: number; rationale: string };
type ReadinessScore = { score: number; evaluated_evaluations: number };
type Report = { assessment_id: string; results: Result[]; findings: Finding[]; readiness_score: ReadinessScore | null; next_cursor: string | null; findings_next_cursor: string | null; coverage: { planned_evaluations: number; completed_evaluations: number; percentage: number } };

const verifierKey = "governance.oauth.pkce.verifier";
const stateKey = "governance.oauth.state";
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
  history.replaceState({}, "", window.location.pathname);
  return token.access_token;
}

function StartAssessment({ accessToken }: { accessToken: string }) {
  const [repositoryId, setRepositoryId] = useState("");
  const [policyProfileId, setPolicyProfileId] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "";
    const response = await fetch(`${baseUrl}/assessments`, { method: "POST", headers: { "content-type": "application/json", Authorization: `Bearer ${accessToken}` }, body: JSON.stringify({ repository_id: repositoryId, policy_profile_id: policyProfileId }) });
    if (!response.ok) throw new Error("Assessment를 시작하지 못했습니다. 승인된 repository/profile scope를 확인하세요.");
    const result = await response.json() as { assessment_id?: unknown };
    if (typeof result.assessment_id !== "string" || !result.assessment_id) throw new Error("Assessment ID를 받지 못했습니다.");
    window.location.assign(`${window.location.pathname}?assessment_id=${encodeURIComponent(result.assessment_id)}`);
  }
  return <main><h1>Initial Assessment</h1><form onSubmit={event => void submit(event).catch((reason: Error) => setError(reason.message))}><label>Repository ID <input required value={repositoryId} onChange={event => setRepositoryId(event.target.value)} /></label><label>Policy Profile ID <input required value={policyProfileId} onChange={event => setPolicyProfileId(event.target.value)} /></label><button type="submit">Assessment 시작</button>{error && <p role="alert">{error}</p>}</form></main>;
}

function AssessmentReport({ assessmentId }: { assessmentId: string }) {
  const [report, setReport] = useState<Report | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [findingsCursor, setFindingsCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [accessToken, setAccessToken] = useState<string | null>(null);
  useEffect(() => { exchangeCallback().then(token => { if (token) setAccessToken(token); }).catch((reason: Error) => setError(reason.message)); }, []);
  useEffect(() => {
    // Without an assessment_id there is nothing to read yet; the start form owns that step.
    if (!accessToken || !assessmentId) return;
    const params = new URLSearchParams({ limit: "25" });
    if (cursor) params.set("cursor", cursor);
    if (findingsCursor) params.set("findings_cursor", findingsCursor);
    const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "";
    fetch(`${baseUrl}/assessments/${encodeURIComponent(assessmentId)}?${params}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
      .then(async response => response.ok ? response.json() as Promise<Report> : Promise.reject(new Error("Assessment 결과를 불러오지 못했습니다.")))
      .then(next => setReport(previous => previous && (cursor || findingsCursor) ? { ...next, results: uniqueResults([...previous.results, ...next.results]), findings: uniqueFindings([...previous.findings, ...next.findings]) } : next))
      .catch((reason: Error) => setError(reason.message));
  }, [accessToken, assessmentId, cursor, findingsCursor]);
  if (!accessToken) return <main><h1>Initial Assessment</h1><p>고객 Cognito 계정으로 로그인해 Assessment 결과를 확인하세요.</p><button onClick={() => void startLogin().catch((reason: Error) => setError(reason.message))}>Cognito로 로그인</button></main>;
  if (!assessmentId) return <StartAssessment accessToken={accessToken} />;
  if (error) return <p role="alert">{error}</p>;
  if (!report) return <p>Assessment 결과를 불러오는 중…</p>;
  return <main><h1>Initial Assessment</h1><section><strong>평가 실행률 {report.coverage.percentage}%</strong><span>{report.coverage.completed_evaluations} / {report.coverage.planned_evaluations} applicable evaluations</span><strong>Readiness Score {report.readiness_score ? report.readiness_score.score : "계산 대기"}</strong></section><h2>Findings ({report.findings.length})</h2><table><thead><tr><th>Resource</th><th>Rule</th><th>Perspective</th><th>Status</th><th>Severity</th><th>Score</th></tr></thead><tbody>{report.findings.map(finding => <tr key={finding.finding_id}><td>{finding.resource_id}</td><td>{finding.rule_id}</td><td>{finding.perspective}</td><td>{finding.status}</td><td>{finding.severity}</td><td>{finding.score}</td></tr>)}</tbody></table>{report.findings_next_cursor && <button onClick={() => setFindingsCursor(report.findings_next_cursor)}>Findings 더 보기</button>}<h2>Evaluation results</h2><table><thead><tr><th>Resource</th><th>Rule</th><th>Perspective</th><th>Status</th><th>Score</th></tr></thead><tbody>{report.results.map(result => <tr key={`${result.resource_id}-${result.rule_id}-${result.perspective}`}><td>{result.resource_id}</td><td>{result.rule_id}</td><td>{result.perspective}</td><td>{result.status}</td><td>{result.score}</td></tr>)}</tbody></table>{report.next_cursor && <button onClick={() => setCursor(report.next_cursor)}>Load more</button>}</main>;
}

function uniqueResults(values: Result[]) { return [...new Map(values.map(value => [`${value.resource_id}:${value.rule_id}:${value.perspective}`, value])).values()]; }
function uniqueFindings(values: Finding[]) { return [...new Map(values.map(value => [value.finding_id, value])).values()]; }

const assessmentId = new URLSearchParams(location.search).get("assessment_id") ?? "";
createRoot(document.getElementById("root")!).render(<StrictMode><AssessmentReport assessmentId={assessmentId} /></StrictMode>);
