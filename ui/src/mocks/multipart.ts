// Read a multipart/form-data request into plain values. Tries the platform's parser first and falls
// back to a small parser of our own: Node's parser rejects the bodies jsdom's XMLHttpRequest writes,
// so component tests would otherwise fail on uploads that work in the browser.

export interface UploadedFile {
  name: string;
  type: string;
  size: number;
  text: string;
}

export interface Form {
  fields: Record<string, string>;
  file: UploadedFile | null;
}

export async function readForm(request: Request): Promise<Form> {
  try {
    const fd = await request.clone().formData();
    const fields: Record<string, string> = {};
    let file: UploadedFile | null = null;
    for (const [k, v] of fd.entries()) {
      if (typeof v === "string") fields[k] = v;
      else if (k === "file") file = { name: (v as File).name, type: v.type, size: v.size, text: await v.text() };
    }
    return { fields, file };
  } catch {
    return parse(request.headers.get("content-type") ?? "", new Uint8Array(await request.arrayBuffer()));
  }
}

function parse(contentType: string, body: Uint8Array): Form {
  const boundary = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType);
  const out: Form = { fields: {}, file: null };
  if (!boundary) return out;
  const raw = new TextDecoder("latin1").decode(body); // byte-preserving
  const delimiter = `--${boundary[1] ?? boundary[2]}`;
  for (const part of raw.split(delimiter).slice(1)) {
    if (part.startsWith("--")) break;
    const sep = part.indexOf("\r\n\r\n");
    if (sep < 0) continue;
    const head = part.slice(0, sep);
    const data = part.slice(sep + 4).replace(/\r\n$/, "");
    const name = /name="([^"]*)"/i.exec(head)?.[1] ?? "";
    const filename = /filename="([^"]*)"/i.exec(head)?.[1];
    const bytes = Uint8Array.from(data, (c) => c.charCodeAt(0));
    const text = new TextDecoder().decode(bytes);
    if (filename !== undefined) out.file = { name: filename, type: /content-type:\s*([^\r\n]+)/i.exec(head)?.[1]?.trim() ?? "", size: bytes.length, text };
    else out.fields[name] = text;
  }
  return out;
}
