import { computed } from "vue";
import type {
  LocationQuery,
  LocationQueryRaw,
  Router,
  RouteLocationNormalizedLoaded,
} from "vue-router";
import type {
  ArtifactReviewStatus,
  ArtifactRiskLevel,
  ReviewCommentSide,
  ReviewWorkspaceView,
} from "@/types/artifacts";

const VIEWS = new Set<ReviewWorkspaceView>([
  "summary",
  "files",
  "diff",
  "comments",
  "history",
  "policy",
]);
const SIDES = new Set<ReviewCommentSide>(["base", "current"]);
const STATUSES = new Set<ArtifactReviewStatus>([
  "quarantined",
  "prechecking",
  "scanning",
  "pending_review",
  "changes_requested",
  "approved",
  "rejected",
  "withdrawn",
  "processing_failed",
]);
const RISKS = new Set<ArtifactRiskLevel>(["none", "low", "medium", "high", "critical"]);

export interface ReviewSelection {
  artifactId: string;
  view: ReviewWorkspaceView;
  fileId: string;
  diffId: string;
  hunkId: string;
  side: ReviewCommentSide;
  lineStart: number | null;
  lineEnd: number | null;
  status: ArtifactReviewStatus | "";
  risk: ArtifactRiskLevel | "";
}

export interface ReviewSelectionPatch {
  artifact?: string;
  view?: ReviewWorkspaceView;
  file?: string;
  diff?: string;
  hunk?: string;
  side?: ReviewCommentSide;
  line?: number;
  line_end?: number;
  status?: ArtifactReviewStatus | "";
  risk?: ArtifactRiskLevel | "";
}

function stringValue(value: LocationQuery[string]): string {
  return typeof value === "string" ? value.trim() : "";
}

function positiveInteger(value: LocationQuery[string]): number | null {
  const raw = stringValue(value);
  if (!/^\d+$/.test(raw)) return null;
  const parsed = Number(raw);
  return Number.isSafeInteger(parsed) && parsed >= 1 ? parsed : null;
}

export function parseReviewSelection(query: LocationQuery): ReviewSelection {
  const view = stringValue(query.view) as ReviewWorkspaceView;
  const side = stringValue(query.side) as ReviewCommentSide;
  const status = stringValue(query.status) as ArtifactReviewStatus;
  const risk = stringValue(query.risk) as ArtifactRiskLevel;
  const lineStart = positiveInteger(query.line);
  const rawLineEnd = positiveInteger(query.line_end);
  return {
    artifactId: stringValue(query.artifact),
    view: VIEWS.has(view) ? view : "summary",
    fileId: stringValue(query.file),
    diffId: stringValue(query.diff),
    hunkId: stringValue(query.hunk),
    side: SIDES.has(side) ? side : "current",
    lineStart,
    lineEnd: rawLineEnd && lineStart ? Math.max(lineStart, rawLineEnd) : lineStart,
    status: STATUSES.has(status) ? status : "",
    risk: RISKS.has(risk) ? risk : "",
  };
}

export function buildReviewQuery(
  current: LocationQuery,
  patch: ReviewSelectionPatch,
): LocationQueryRaw {
  const query: LocationQueryRaw = { ...current };
  for (const [key, value] of Object.entries(patch)) {
    query[key] = value === "" || value == null ? undefined : String(value);
  }
  return query;
}

export function useReviewSelection(route: RouteLocationNormalizedLoaded, router: Router) {
  const selection = computed(() => parseReviewSelection(route.query));

  async function replace(patch: ReviewSelectionPatch): Promise<void> {
    await router.replace({ query: buildReviewQuery(route.query, patch) });
  }

  function setArtifact(artifactId: string): Promise<void> {
    return replace({
      artifact: artifactId,
      view: "summary",
      file: "",
      diff: "",
      hunk: "",
      side: "current",
      line: undefined,
      line_end: undefined,
    });
  }

  function setView(view: ReviewWorkspaceView): Promise<void> {
    return replace({ view });
  }

  function setStatus(status: ArtifactReviewStatus | ""): Promise<void> {
    return replace({
      status,
      artifact: "",
      view: "summary",
      file: "",
      diff: "",
      hunk: "",
      line: undefined,
      line_end: undefined,
    });
  }

  function setRisk(risk: ArtifactRiskLevel | ""): Promise<void> {
    return replace({
      risk,
      artifact: "",
      view: "summary",
      file: "",
      diff: "",
      hunk: "",
      line: undefined,
      line_end: undefined,
    });
  }

  function selectFile(fileId: string, lineStart?: number, lineEnd?: number): Promise<void> {
    return replace({
      view: "files",
      file: fileId,
      diff: "",
      hunk: "",
      side: "current",
      line: lineStart,
      line_end: lineEnd,
    });
  }

  function selectDiff(diffId: string): Promise<void> {
    return replace({
      view: "diff",
      diff: diffId,
      hunk: "",
      line: undefined,
      line_end: undefined,
    });
  }

  function selectLine(input: {
    fileId: string;
    side: ReviewCommentSide;
    lineStart: number;
    lineEnd?: number;
    diffId?: string;
    hunkId?: string;
  }): Promise<void> {
    return replace({
      file: input.fileId,
      side: input.side,
      line: input.lineStart,
      line_end: input.lineEnd ?? input.lineStart,
      diff: input.diffId ?? "",
      hunk: input.hunkId ?? "",
    });
  }

  return {
    selection,
    replace,
    setArtifact,
    setView,
    setStatus,
    setRisk,
    selectFile,
    selectDiff,
    selectLine,
  };
}
