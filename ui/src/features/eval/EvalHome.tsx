import { FlaskConical, Play, Upload } from "lucide-react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { useDatasets, useEvalRuns, usePipelines } from "../../api/hooks";
import { Button } from "../../components/Button";
import { EmptyState, ErrorState, SkeletonRows } from "../../components/States";
import { Meter, StatusIndicator } from "../../components/Status";
import { Time } from "../../components/Time";
import { count } from "../../lib/format";
import { useToast } from "../../lib/toast";
import { NewRun } from "./NewRun";
import { UploadDataset } from "./UploadDataset";

const RUN_STATE = { pending: "pending", running: "running", finished: "succeeded", failed: "failed" } as const;

/** Datasets and runs. Uploading and starting a run happen in inline panels. */
export function EvalHome() {
  const { corpusId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const toast = useToast();
  const datasets = useDatasets(corpusId);
  const runs = useEvalRuns(corpusId);
  const pipelines = usePipelines(corpusId);
  const panel = params.get("panel") ?? (params.get("compare") ? "run" : null);
  const base = `/corpora/${corpusId}/eval`;
  const setPanel = (p: string | null) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (p) next.set("panel", p);
        else {
          next.delete("panel");
          next.delete("compare");
        }
        return next;
      },
      { replace: true },
    );
  const ds = datasets.data?.items ?? [];
  const nameOf = (id: string) => pipelines.data?.items.find((p) => p.id === id)?.name ?? "(archived)";

  return (
    <div className="page-pad eval">
      <header className="page-head">
        <div>
          <h1>Evaluation</h1>
          <p className="page-head__sub">Ask the same questions of several pipelines and see, with confidence intervals, which one finds the answers.</p>
        </div>
        <span className="page-head__spacer" />
        <Button icon={Upload} onClick={() => setPanel(panel === "upload" ? null : "upload")} aria-expanded={panel === "upload"}>
          Upload dataset
        </Button>
        <Button variant="primary" icon={Play} disabled={!ds.length} title={ds.length ? undefined : "Upload a dataset first"} onClick={() => setPanel(panel === "run" ? null : "run")} aria-expanded={panel === "run"}>
          New run
        </Button>
      </header>

      {panel === "upload" ? (
        <UploadDataset
          corpusId={corpusId}
          onCancel={() => setPanel(null)}
          onDone={(d) => {
            toast({ tone: "ok", title: `Imported ${d.name}`, body: `${count(d.item_count)} questions.` });
            setPanel(null);
          }}
        />
      ) : null}
      {panel === "run" && datasets.data && ds.length ? (
        <NewRun
          corpusId={corpusId}
          datasets={ds}
          initialDataset={params.get("dataset")}
          initialPipelines={(params.get("compare") ?? "").split(",").filter(Boolean)}
          onCancel={() => setPanel(null)}
          onDone={(r) => navigate(`${base}/runs/${r.id}`)}
        />
      ) : null}

      <section className="eval-section" aria-labelledby="runs-title">
        <h2 id="runs-title">Runs</h2>
        {runs.isError && !runs.data ? (
          <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
        ) : runs.data && !runs.data.items.length ? (
          <EmptyState icon={FlaskConical} title="No runs yet">
            <p>{ds.length ? "Start a run to compare pipelines on a dataset." : "Upload a dataset of questions first, then start a run."}</p>
          </EmptyState>
        ) : (
          <div className="panel">
            <table className="rc-table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Pipelines</th>
                  <th>Status</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {!runs.data ? (
                  <SkeletonRows rows={2} cols={[160, 260, 160, 80]} />
                ) : (
                  runs.data.items.map((r) => (
                    <tr key={r.id}>
                      <td>
                        <Link to={`${base}/runs/${r.id}`} className="strong-link">
                          {r.dataset_name}
                        </Link>{" "}
                        <span className="muted mono">k {r.k_values.join(",")}</span>
                      </td>
                      <td className="mono">{r.pipeline_ids.map(nameOf).join(" · ")}</td>
                      <td>
                        {r.status === "running" || r.status === "pending" ? (
                          <Meter value={r.progress.total ? r.progress.done / r.progress.total : 0} tone="warn" label={`${count(r.progress.done)}/${count(r.progress.total)} queries`} />
                        ) : (
                          <StatusIndicator state={RUN_STATE[r.status]} label={r.status === "finished" ? "Finished" : "Failed"} />
                        )}
                      </td>
                      <td className="muted nowrap">
                        <Time iso={r.created_at} />
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="eval-section" aria-labelledby="datasets-title">
        <h2 id="datasets-title">Datasets</h2>
        {datasets.isError && !datasets.data ? (
          <ErrorState error={datasets.error} onRetry={() => void datasets.refetch()} />
        ) : datasets.data && !ds.length ? (
          <EmptyState
            icon={Upload}
            title="No datasets yet"
            action={
              <Button variant="primary" icon={Upload} onClick={() => setPanel("upload")}>
                Upload dataset
              </Button>
            }
          >
            <p>A dataset is a list of questions, each naming the documents (and optionally pages or text) that answer it.</p>
          </EmptyState>
        ) : (
          <div className="panel">
            <table className="rc-table">
              <thead>
                <tr>
                  <th>Dataset</th>
                  <th className="rc-right">Questions</th>
                  <th>Imported</th>
                </tr>
              </thead>
              <tbody>
                {!datasets.data ? (
                  <SkeletonRows rows={2} cols={[180, 60, 80]} />
                ) : (
                  ds.map((d) => (
                    <tr key={d.id}>
                      <td>
                        <Link to={`${base}/datasets/${d.id}`} className="strong-link mono">
                          {d.name}
                        </Link>
                      </td>
                      <td className="rc-right num mono">{count(d.item_count)}</td>
                      <td className="muted">
                        <Time iso={d.created_at} />
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
