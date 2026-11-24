export type RunWith = "docker" | "uv" | "remote";

const cpuTorch = "uv venv && uv pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision";

// Keep IDs and ports aligned with KNOWN_SHIMS in src/ragcentre/ocr/addresses.py.
export const ENGINES = [
  {
    id: "docling",
    label: "Docling",
    port: 8701,
    hardware: "CPU is fine",
    summary: "PDFs, Office files, HTML and images, with layout and tables. A good default.",
    note: "The first parse is slow while Docling downloads its models.",
  },
  {
    id: "mineru",
    label: "MinerU",
    port: 8702,
    hardware: "Faster on a GPU",
    summary: "Layout-heavy PDFs, formulas and tables.",
  },
  {
    id: "paddleocr",
    label: "PaddleOCR",
    port: 8703,
    hardware: "CPU is fine",
    summary: "PDFs and images with OCR, layout, tables and formulas.",
    cache: ".paddlex",
    afterSync: "uv pip install paddlepaddle",
    note: "The command installs PaddlePaddle for CPU. See the shim README for GPU builds.",
  },
  {
    id: "surya",
    label: "Surya",
    port: 8704,
    hardware: "Needs an inference server",
    summary: "OCR and layout from a vision model.",
    note: "Surya starts an inference server on first use. See examples/surya_shim/README.md for prerequisites.",
  },
  {
    id: "easyocr",
    label: "EasyOCR",
    port: 8705,
    hardware: "CPU is fine",
    summary: "Text lines in many languages, without layout analysis.",
    cache: ".EasyOCR",
    beforeSync: cpuTorch,
  },
  {
    id: "doctr",
    label: "docTR",
    port: 8706,
    hardware: "CPU is fine",
    summary: "Words and text lines, best for Latin-script scans.",
    beforeSync: cpuTorch,
  },
] as const;
export type EngineId = (typeof ENGINES)[number]["id"];
type Engine = {
  id: EngineId;
  port: number;
  beforeSync?: string;
  afterSync?: string;
  cache?: string;
  note?: string;
};

export function shimCommand(engine: Engine, runWith: RunWith, inContainer: boolean) {
  const { id, port } = engine;
  const dir = `examples/${id}_shim`;
  const notes = engine.note ? [engine.note] : [];
  const localUrl = `http://127.0.0.1:${port}`;
  if (runWith === "docker") {
    if (inContainer) {
      notes.unshift("Run it where RAGCENTRE_API_TOKEN is set, as when you started RAGCentre.");
      return { lines: [`docker compose --profile ${id} up -d --build ${id}`], watchUrl: `http://${id}:${port}`, notes };
    }
    return {
      lines: [
        `docker build -t ${id}-shim -f ${dir}/Dockerfile examples &&`,
        `docker run -d --name ${id}-shim -p 127.0.0.1:${port}:${port} -v ${id}-models:/home/shim/${engine.cache ?? ".cache"} ${id}-shim`,
      ],
      watchUrl: localUrl,
      notes,
    };
  }

  const remote = runWith === "remote";
  const bindAll = remote || inContainer;
  const command = [
    `cd ${dir}`,
    ...(engine.beforeSync ? [engine.beforeSync] : []),
    "uv sync",
    ...(engine.afterSync ? [engine.afterSync] : []),
    `uv run${engine.afterSync ? " --no-sync" : ""} uvicorn ${id}_shim.app:app --host ${bindAll ? "0.0.0.0" : "127.0.0.1"} --port ${port}`,
  ];
  if (remote) notes.unshift("Run this on the computer hosting the shim, then enter its address below.");
  if (bindAll) notes.push(`This listens on every network interface. Set ${id.toUpperCase()}_SHIM_API_KEY or use a firewall on a shared network.`);
  if (engine.beforeSync) notes.push("The PyTorch CPU install is optional. Omit the uv venv and uv pip install line on macOS or on a machine using an NVIDIA GPU.");
  return {
    lines: [`( ${command.join(" &&\n  ")} )`],
    watchUrl: remote ? null : inContainer ? `http://host.docker.internal:${port}` : localUrl,
    notes,
  };
}

export function urlPort(url: unknown): number | undefined {
  if (typeof url !== "string") return undefined;
  try {
    return Number(new URL(url).port) || undefined;
  } catch {
    return undefined;
  }
}
