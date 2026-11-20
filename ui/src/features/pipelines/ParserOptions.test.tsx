import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { optionSpecs } from "./optionsSchema";
import { ParserOptions } from "./ParserOptions";

const schema = {
  type: "object",
  additionalProperties: false,
  properties: {
    ocr: { type: "boolean", default: true, description: "Run OCR" },
    table_mode: { type: "string", enum: ["accurate", "fast"], default: "accurate" },
    dpi: { type: "integer", minimum: 72, maximum: 400, default: 192 },
    prompt: { type: "string" },
    page_range: { type: "array", items: { type: "integer" }, minItems: 2, maxItems: 2 },
    ocr_mode: { enum: [true, false, "auto"] },
  },
};

describe("optionSpecs", () => {
  it("reads each kind of option", () => {
    const specs = optionSpecs(schema)!;
    expect(specs.map((s) => [s.key, s.control.kind])).toEqual([
      ["ocr", "boolean"],
      ["table_mode", "enum"],
      ["dpi", "integer"],
      ["prompt", "string"],
      ["page_range", "json"],
      ["ocr_mode", "json"],
    ]);
    expect(specs[2]!.control).toMatchObject({ min: 72, max: 400 });
    expect(specs[0]!.default).toBe(true);
  });
  it("gives up on schemas it cannot show", () => {
    for (const odd of [undefined, null, "x", [], {}, { properties: {} }, { properties: [] }]) expect(optionSpecs(odd)).toBeNull();
  });
});

function Harness({ initial, problems = [] }: { initial?: Record<string, unknown>; problems?: { path: string; message: string }[] }) {
  const [value, setValue] = useState<Record<string, unknown> | undefined>(initial);
  return (
    <>
      <ParserOptions specs={optionSpecs(schema)!} value={value} onChange={setValue} problems={problems} asJson={<p>json editor</p>} />
      <output data-testid="value">{JSON.stringify(value ?? null)}</output>
    </>
  );
}
const value = () => JSON.parse(screen.getByTestId("value").textContent!);

describe("ParserOptions", () => {
  it("sends only the options that were set", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    expect(value()).toBeNull();
    const ocr = screen.getByRole("checkbox", { name: "ocr" });
    expect(ocr).toBeChecked(); // shows the default
    await user.click(ocr);
    expect(value()).toEqual({ ocr: false });
    await user.selectOptions(screen.getByLabelText("table_mode"), "fast");
    await user.type(screen.getByLabelText("dpi"), "300");
    expect(value()).toEqual({ ocr: false, table_mode: "fast", dpi: 300 });
  });

  it("resets an option back to the parser's default", async () => {
    const user = userEvent.setup();
    render(<Harness initial={{ ocr: false }} />);
    await user.click(screen.getByRole("button", { name: "Reset ocr" }));
    expect(value()).toBeNull();
  });

  it("does not send a number outside the limits", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("dpi"), "10");
    expect(value()).toBeNull();
    expect(screen.getByText(/Enter a value 72–400/)).toBeInTheDocument();
  });

  it("shows a key the parser does not list, and lets you remove it", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<Harness initial={{ tabels: false }} />);
    expect(screen.getByText("This parser does not list this option.")).toBeInTheDocument();
    unmount();
    render(<Harness initial={{ tabels: false }} problems={[{ path: ".tabels", message: 'Unknown option "tabels".' }]} />);
    expect(screen.getByText('Unknown option "tabels".')).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove tabels" }));
    expect(value()).toBeNull();
  });

  it("edits lists as JSON values and reports the server's problems beside the option", async () => {
    const user = userEvent.setup();
    render(<Harness problems={[{ path: ".dpi", message: "Must be at least 72." }]} />);
    expect(screen.getByText("Must be at least 72.")).toBeInTheDocument();
    const box = screen.getByLabelText("page_range");
    await user.type(box, "[[2, 4]");
    await user.tab();
    expect(value()).toEqual({ page_range: [2, 4] });
  });

  it("switches to the JSON editor", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Edit as JSON" }));
    expect(screen.getByText("json editor")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit as form" }));
    expect(screen.getByRole("group", { name: "Parser options" })).toBeInTheDocument();
  });
});
