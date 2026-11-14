// Start empty: the first-run acceptance test must exercise setup in the UI.
import { api } from './api';

export default async function globalSetup(): Promise<void> {
  const corpora = await api<{ items: unknown[] }>('GET', '/corpora');
  if (corpora.items.length) throw new Error('Live acceptance requires an empty, isolated data directory');
}
