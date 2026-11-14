import "@testing-library/jest-dom/vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { uuidFrom } from "../../mocks/hash";
import { renderApp } from "../../test/render";

const support = uuidFrom("corpus:support-docs");

describe("corpora (UI-1)", () => {
  it("creates a corpus with a metadata field and opens it", async () => {
    const user = userEvent.setup();
    const { router, state } = renderApp("/corpora/new", "empty");
    await user.type(await screen.findByLabelText(/^Name/), "handbook");
    await user.click(screen.getByRole("button", { name: "Add field" }));
    await user.type(screen.getByLabelText("New field 1 name"), "lang");
    await user.type(screen.getByLabelText("New field 1 default"), "en");
    await user.click(screen.getByRole("button", { name: "Create corpus" }));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/corpora\/[0-9a-f-]+\/documents$/));
    expect(state.corpora.find((c) => c.name === "handbook")?.metadata_schema).toEqual({ fields: { lang: { type: "str", default: "en" } } });
  });

  it("rejects a reserved field name and a missing default", async () => {
    const user = userEvent.setup();
    renderApp("/corpora/new", "empty");
    await user.type(await screen.findByLabelText(/^Name/), "x");
    await user.click(screen.getByRole("button", { name: "Add field" }));
    await user.type(screen.getByLabelText("New field 1 name"), "document_id");
    await user.click(screen.getByRole("button", { name: "Add field" }));
    await user.type(screen.getByLabelText("New field 2 name"), "year");
    await user.selectOptions(screen.getByLabelText("New field 2 type"), "int");
    await user.click(screen.getByRole("button", { name: "Create corpus" }));
    expect(await screen.findByText(/document_id is reserved/)).toBeInTheDocument();
    expect(screen.getByText("Give a default integer.")).toBeInTheDocument();
  });

  it("adds a schema field in settings; existing fields cannot be edited or removed", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`/corpora/${support}/settings`);
    const section = (await screen.findByRole("heading", { name: "Metadata fields" })).closest("form")!;
    const langRow = within(section).getByText("lang").closest("tr")!;
    expect(within(langRow).queryByRole("textbox")).toBeNull();
    expect(within(langRow).queryByRole("button")).toBeNull();
    expect(within(langRow).getByLabelText("Cannot be changed")).toBeInTheDocument();
    await user.click(within(section).getByRole("button", { name: "Add field" }));
    await user.type(within(section).getByLabelText("New field 1 name"), "internal");
    await user.selectOptions(within(section).getByLabelText("New field 1 type"), "bool");
    await user.click(within(section).getByRole("button", { name: "Save new field" }));
    await waitFor(() => expect(state.corpora.find((c) => c.id === support)!.metadata_schema.fields.internal).toEqual({ type: "bool", default: false }));
    expect(state.corpora.find((c) => c.id === support)!.metadata_schema.fields.lang).toEqual({ type: "str", default: "en" });
  });

  it("renames a corpus and refuses a duplicate name", async () => {
    const user = userEvent.setup();
    const { state } = renderApp(`/corpora/${support}/settings`);
    const input = await screen.findByLabelText("Corpus name");
    await user.clear(input);
    await user.type(input, "eng-wiki");
    await user.click(screen.getByRole("button", { name: "Rename" }));
    expect(await screen.findByText("A corpus named 'eng-wiki' already exists.")).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "help-centre");
    await user.click(screen.getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(state.corpora.find((c) => c.id === support)!.name).toBe("help-centre"));
  });
});
