import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { connectorScopeSchemas } from "../../mocks/scopeSchemas";
import { ScheduleFields, ScopeFields } from "./SourceFields";
import { broadScopeMessage, scopeProblem, type ScopeSchema } from "./scopeModel";

function ScopeHarness({ initial, schema = connectorScopeSchemas.s3! }: { initial: Record<string, unknown>; schema?: ScopeSchema | null }) {
  const [value, setValue] = useState(JSON.stringify(initial));
  const [confirmed, setConfirmed] = useState(false);
  return <>
    <ScopeFields value={value} onChange={setValue} schema={schema} confirmed={confirmed} onConfirm={setConfirmed} />
    <output aria-label="Saved scope">{value}</output>
  </>;
}
function ScheduleHarness({ initial = "3600" }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return <><ScheduleFields value={value} onChange={setValue} /><output aria-label="Saved seconds">{value}</output></>;
}

describe("supported source fields", () => {
  it("round-trips nested extension fields while editing supported fields and advanced JSON", async () => {
    const user = userEvent.setup();
    render(<ScopeHarness initial={{ bucket: "docs", prefix: "old/", future: { nested: [1, null, true] } }} />);
    const prefix = screen.getByLabelText(/^Prefix/);
    await user.clear(prefix);
    await user.type(prefix, "new/");
    const advanced = screen.getByRole("checkbox", { name: "Advanced scope JSON" });
    await user.click(advanced);
    expect(JSON.parse((screen.getByLabelText("Scope JSON") as HTMLTextAreaElement).value)).toEqual({ bucket: "docs", prefix: "new/", future: { nested: [1, null, true] } });
    await user.click(advanced);
    expect(screen.getByLabelText(/^Bucket/)).toHaveValue("docs");
    expect(JSON.parse(screen.getByLabelText("Saved scope").textContent!)).toEqual({ bucket: "docs", prefix: "new/", future: { nested: [1, null, true] } });
  });

  it("retains plugin scope JSON without imposing bundled connector fields", () => {
    render(<ScopeHarness initial={{ query: ["one", { nested: true }] }} schema={null} />);
    expect(screen.queryByRole("checkbox", { name: "Advanced scope JSON" })).toBeNull();
    expect(JSON.parse((screen.getByLabelText("Scope JSON") as HTMLTextAreaElement).value)).toEqual({ query: ["one", { nested: true }] });
    expect(scopeProblem('{"query":[1]}', null)).toBeUndefined();
  });

  it("requires fresh broad-scope confirmation after the scope changes", async () => {
    const user = userEvent.setup();
    render(<ScopeHarness initial={{ bucket: "docs" }} />);
    const confirm = screen.getByRole("checkbox", { name: "I confirm this broad import scope" });
    expect(confirm).not.toBeChecked();
    await user.click(confirm);
    await user.type(screen.getByLabelText(/^Bucket/), "-other");
    expect(confirm).not.toBeChecked();
    await user.type(screen.getByLabelText(/^Prefix/), "manuals/");
    expect(screen.queryByRole("checkbox", { name: "I confirm this broad import scope" })).toBeNull();
  });

  it("keeps invalid legacy field values visible in advanced JSON", () => {
    render(<ScopeHarness initial={{ bucket: "docs", prefix: 123, future: "kept" }} />);
    expect(JSON.parse((screen.getByLabelText("Scope JSON") as HTMLTextAreaElement).value)).toEqual({ bucket: "docs", prefix: 123, future: "kept" });
    expect(scopeProblem('{"bucket":"docs","prefix":123}', connectorScopeSchemas.s3!)).toBe("Prefix must be a string or null.");
  });

  it.each([
    ["s3", {}, "Bucket is required."],
    ["sharepoint", {}, "Drive ID is required."],
    ["confluence", {}, "Space key is required."],
    ["gdrive", { folder_id: 1 }, "Folder ID must be a string or null."],
    ["notion", { database_id: false }, "Database ID must be a string or null."],
  ])("validates %s supported fields", (type, scope, problem) => {
    expect(scopeProblem(JSON.stringify(scope), connectorScopeSchemas[type as string]!)).toBe(problem);
  });

  it("explains broad Drive and Notion scopes while accepting narrow scopes", () => {
    expect(broadScopeMessage("{}", connectorScopeSchemas.gdrive!)).toContain("all files visible");
    expect(broadScopeMessage('{"shared_drive_id":"drive"}', connectorScopeSchemas.gdrive!)).toBeTruthy();
    expect(broadScopeMessage('{"folder_id":"folder"}', connectorScopeSchemas.gdrive!)).toBeUndefined();
    expect(broadScopeMessage("{}", connectorScopeSchemas.notion!)).toContain("all pages");
    expect(broadScopeMessage('{"database_id":"db"}', connectorScopeSchemas.notion!)).toBeUndefined();
  });

  it("maps Manual, Hourly and Daily to their API values and retains custom intervals", async () => {
    const user = userEvent.setup();
    render(<ScheduleHarness initial="12345" />);
    expect(screen.getByLabelText("Schedule")).toHaveValue("custom");
    expect(screen.getByLabelText("Custom interval (seconds)")).toHaveValue("12345");
    for (const [preset, seconds] of [["manual", "0"], ["hourly", "3600"], ["daily", "86400"]]) {
      await user.selectOptions(screen.getByLabelText("Schedule"), preset!);
      expect(screen.getByLabelText("Saved seconds")).toHaveTextContent(seconds!);
    }
    await user.selectOptions(screen.getByLabelText("Schedule"), "custom");
    const custom = screen.getByLabelText("Custom interval (seconds)");
    await user.clear(custom);
    await user.type(custom, "7201");
    expect(screen.getByLabelText("Saved seconds")).toHaveTextContent("7201");
    expect(screen.getByLabelText("Schedule")).toHaveValue("custom");
  });
});
