import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Result = { resource_id: string; rule_id: string; perspective: string; status: string; score: number; severity: string; rationale: string };
type Report = { assessment_id: string; results: Result[]; next_cursor: string | null; coverage: { planned_evaluations: number; completed_evaluations: number; percentage: number } };

function AssessmentReport({ assessmentId }: { assessmentId: string }) {
  const [report, setReport] = useState<Report | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const params = new URLSearchParams({ limit: "25" });
    if (cursor) params.set("cursor", cursor);
    fetch(`/assessments/${encodeURIComponent(assessmentId)}?${params}`)
      .then(async response => response.ok ? response.json() as Promise<Report> : Promise.reject(new Error("Assessment 결과를 불러오지 못했습니다.")))
      .then(next => setReport(previous => previous && cursor ? { ...next, results: [...previous.results, ...next.results] } : next))
      .catch((reason: Error) => setError(reason.message));
  }, [assessmentId, cursor]);
  if (error) return <p role="alert">{error}</p>;
  if (!report) return <p>Assessment 결과를 불러오는 중…</p>;
  return <main><h1>Initial Assessment</h1><section><strong>Coverage {report.coverage.percentage}%</strong><span>{report.coverage.completed_evaluations} / {report.coverage.planned_evaluations} applicable evaluations</span></section><table><thead><tr><th>Resource</th><th>Rule</th><th>Perspective</th><th>Status</th><th>Score</th></tr></thead><tbody>{report.results.map(result => <tr key={`${result.resource_id}-${result.rule_id}-${result.perspective}`}><td>{result.resource_id}</td><td>{result.rule_id}</td><td>{result.perspective}</td><td>{result.status}</td><td>{result.score}</td></tr>)}</tbody></table>{report.next_cursor && <button onClick={() => setCursor(report.next_cursor)}>Load more</button>}</main>;
}

const assessmentId = new URLSearchParams(location.search).get("assessment_id") ?? "";
createRoot(document.getElementById("root")!).render(<StrictMode>{assessmentId ? <AssessmentReport assessmentId={assessmentId} /> : <p>assessment_id is required.</p>}</StrictMode>);
