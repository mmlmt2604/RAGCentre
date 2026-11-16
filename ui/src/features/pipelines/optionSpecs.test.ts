import { describe, expect, it } from "vitest";
// The options_schema each shim reports (0.3.0), as tests/fixtures/option_problems.json keeps them.
import table from "../../../../tests/fixtures/option_problems.json";
import { optionSpecs, type Control } from "./optionSpecs";

const kinds = (name: "docling" | "mineru" | "paddleocr" | "surya") =>
  Object.fromEntries(optionSpecs(table.schemas[name])!.map((s) => [s.key, s.control.kind]));

describe("the control chosen for each option of the four parser schemas", () => {
  it("docling", () => {
    expect(kinds("docling")).toEqual({
      pipeline: "enum", vlm_model: "enum", ocr: "boolean", include_page_images: "boolean",
      page_range: "pair", languages: "strings", tables: "boolean", table_mode: "enum", ocr_engine: "enum",
      force_full_page_ocr: "boolean", formulas: "boolean", code: "boolean", picture_classification: "boolean",
      picture_description: "boolean", charts: "boolean", images_scale: "number", engine: "json",
    });
  });
  it("mineru: a boolean-or-\"auto\" union is a choice", () => {
    expect(kinds("mineru")).toEqual({ tier: "enum", ocr: "choice", image_analysis: "boolean", page_range: "pair" });
    expect(optionSpecs(table.schemas.mineru)!.find((s) => s.key === "ocr")!.control).toEqual({ kind: "choice", choices: [true, false, "auto"] });
  });
  it("paddleocr: nullable booleans are choices, a nullable enum is an enum, a one-item list is a string list", () => {
    expect(kinds("paddleocr")).toEqual({
      tables: "boolean", formulas: "boolean", charts: "boolean", seals: "boolean", orientation: "boolean", unwarp: "boolean",
      heading_levels: "boolean", languages: "strings", ocr_version: "enum", textline_orientation: "choice", region_detection: "choice", engine: "json",
    });
    const spec = (key: string) => optionSpecs(table.schemas.paddleocr)!.find((s) => s.key === key)!.control;
    expect(spec("textline_orientation")).toEqual({ kind: "choice", choices: [true, false] });
    expect(spec("ocr_version")).toEqual({ kind: "enum", choices: ["PP-OCRv3", "PP-OCRv4", "PP-OCRv5"] });
  });
  it("surya: an integer keeps its range", () => {
    expect(kinds("surya")).toEqual({ include_page_images: "boolean", ocr_figures: "boolean", dpi: "integer", page_range: "pair" });
    expect(optionSpecs(table.schemas.surya)!.find((s) => s.key === "dpi")!.control).toMatchObject({ min: 72, max: 400 });
  });
  it("keeps an exclusive limit, and the minimum of the items of a pair", () => {
    const docling = optionSpecs(table.schemas.docling)!;
    expect(docling.find((s) => s.key === "images_scale")!.control).toMatchObject({ kind: "number", exclusiveMin: 0, max: 4 });
    expect(docling.find((s) => s.key === "page_range")!.control).toEqual({ kind: "pair", min: 1 });
  });
});

describe("what falls back to a JSON box", () => {
  const control = (property: object): Control => optionSpecs({ properties: { x: property } })![0]!.control;
  it("unions of more than one real alternative, nested objects and mixed enums", () => {
    expect(control({ anyOf: [{ type: "integer" }, { type: "string" }] })).toEqual({ kind: "json" });
    expect(control({ anyOf: [{ type: "object" }, { type: "null" }] })).toEqual({ kind: "json" });
    expect(control({ enum: ["a", 1] })).toEqual({ kind: "json" });
    expect(control({ type: "array", items: { type: "integer" } })).toEqual({ kind: "json" });
    expect(control({ type: "array", items: { type: "integer" }, minItems: 3, maxItems: 3 })).toEqual({ kind: "json" });
  });
  it("a two-number list is a pair only for page_range: another one (a width and a height) stays JSON", () => {
    const pair = { anyOf: [{ type: "array", items: { type: "integer", minimum: 1 }, minItems: 2, maxItems: 2 }, { type: "null" }] };
    const kind = (key: string) => optionSpecs({ properties: { [key]: pair } })![0]!.control.kind;
    expect(kind("page_range")).toBe("pair");
    expect(kind("size")).toBe("json");
  });
  it("a nullable value is the control of the value: unset is how the form says null", () => {
    expect(control({ anyOf: [{ type: "integer", minimum: 1 }, { type: "null" }] })).toEqual({ kind: "integer", min: 1, max: undefined, exclusiveMin: undefined, exclusiveMax: undefined });
    expect(control({ anyOf: [{ type: "string" }, { type: "null" }] })).toEqual({ kind: "string" });
    expect(control({ anyOf: [{ type: "null" }] })).toEqual({ kind: "json" });
  });
});
