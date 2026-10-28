// One durable outbox per album. A retry repeats the exact operation, including its
// original revision, so a lost response cannot overwrite another editor's work.
const copy = value => JSON.parse(JSON.stringify(value));
const PREFIX = 'album-studio-outbox-v1:';
export class AlbumSaver {
  constructor({storage, send, clientId = 'default', changed = () => {}, saved = () => {}, schedule = (callback, delay) => setTimeout(callback, delay), cancel = handle => clearTimeout(handle)}) {
    Object.assign(this, {storage, send, clientId, changed, saved, schedule, cancel});
    this.entries = new Map();
  }
  key(id) { return PREFIX + id + ':' + this.clientId; }
  open(project) {
    let entry = this.entries.get(project.id);
    if (!entry) {
      let stored;
      try { stored = JSON.parse(this.storage.getItem(this.key(project.id)) || 'null'); }
      catch { throw new Error('Could not read your saved PC draft. Keep this tab open and check browser storage.'); }
      entry = stored || {revision: project.revision, latest: null, flight: null, generation: 0};
      entry.status = entry.latest ? 'pending' : 'saved';
      this.entries.set(project.id, entry);
    }
    if (!entry.latest) entry.revision = project.revision;
    else if (!entry.flight && entry.revision !== project.revision) {
      entry.status = 'conflict';
      entry.error = new Error('The saved album changed in another tab. Your PC draft is kept here.');
    }
    const result = entry.latest ? copy(entry.latest) : project;
    if (entry.latest && entry.status !== 'conflict') this.run(project.id);
    return result;
  }
  persist(id, entry) {
    try {
      this.storage.setItem(this.key(id), JSON.stringify({revision: entry.revision, latest: entry.latest,
        flight: entry.flight, generation: entry.generation}));
    } catch {
      const error = new Error('Browser storage is full or unavailable. Keep this tab open and download your draft.');
      error.storage = true;
      throw error;
    }
  }
  edit(project) {
    const entry = this.entries.get(project.id);
    if (!entry) throw new Error('Open the album before editing it');
    entry.latest = copy(project);
    entry.generation++;
    try { this.persist(project.id, entry); }
    catch (error) { entry.status = 'error'; entry.error = error; this.changed(project.id); return; }
    if (entry.status !== 'conflict') { entry.status = 'pending'; entry.error = null; this.run(project.id); }
    this.changed(project.id);
  }
  run(id) {
    const entry = this.entries.get(id);
    if (!entry || entry.running || !entry.latest || entry.status === 'conflict') return entry?.running;
    this.cancel(entry.timer);
    entry.running = this.pump(id, entry).finally(() => { entry.running = null; });
    // Background autosave never leaves a rejected promise behind.
    return entry.running;
  }
  async pump(id, entry) {
    try {
      while (entry.latest) {
        if (!entry.flight) entry.flight = {generation: entry.generation,
          body: {...copy(entry.latest), revision: entry.revision, _operation_id: crypto.randomUUID()}};
        this.persist(id, entry); // Never send a write whose retry identity isn't durable.
        entry.status = 'saving'; this.changed(id);
        const result = await this.send(id, entry.flight.body);
        entry.revision = result.revision;
        if (entry.flight.generation === entry.generation) entry.latest = null;
        entry.flight = null;
        if (entry.latest) entry.latest.revision = result.revision;
        entry.attempts = 0;
        this.persist(id, entry);
        this.saved(id, result);
      }
      entry.status = 'saved'; entry.error = null;
    } catch (error) {
      entry.error = error;
      entry.status = error.status === 409 ? 'conflict' : 'error';
      // A definitive validation rejection did not commit. Let the next edit
      // correct it; retry identities are retained only for ambiguous failures.
      if ([400, 413, 415, 422].includes(error.status)) {
        entry.flight = null;
        try { this.persist(id, entry); } catch (storageError) { entry.error = storageError; }
      }
      if (!error.storage && (!error.status || error.status >= 500)) {
        const delay = Math.min(30000, 1000 * 2 ** Math.min(entry.attempts || 0, 5));
        entry.attempts = (entry.attempts || 0) + 1;
        entry.timer = this.schedule(() => this.run(id), delay);
      }
    }
    this.changed(id);
  }
  async flush(id) {
    const entry = this.entries.get(id);
    if (!entry) return;
    if (entry.status !== 'conflict') await this.run(id);
    if (entry.latest || entry.error) throw entry.error || new Error('This album still has unsaved changes.');
  }
  status(id) { return this.entries.get(id)?.status || 'saved'; }
  message(id) { return this.entries.get(id)?.error?.message || ''; }
  dirty() { return [...this.entries.values()].some(entry => entry.latest || entry.flight); }
  draft(id) { const draft = this.entries.get(id)?.latest; return draft ? copy(draft) : null; }
  fork(clientId) {
    this.clientId = clientId;
    for (const [id, entry] of this.entries) if (entry.latest) this.persist(id, entry);
  }
  recoverable(id) {
    const drafts = [];
    for (let index = 0; index < this.storage.length; index++) {
      const key = this.storage.key(index);
      if (!key?.startsWith(PREFIX + id + ':') || key === this.key(id)) continue;
      try {
        const value = JSON.parse(this.storage.getItem(key));
        const draft = value.latest || (value.id === id ? value : null);
        if (draft) drafts.push({key, title: draft.title});
      } catch { /* Preserve unreadable records for manual recovery. */ }
    }
    return drafts;
  }
  recover(id, key, project) {
    if (this.entries.get(id)?.latest) throw new Error('Download or resolve the current PC draft first.');
    if (!this.recoverable(id).some(draft => draft.key === key)) throw new Error('PC draft is unavailable');
    const value = JSON.parse(this.storage.getItem(key));
    const entry = value.latest ? value : {revision:value.revision,latest:value,flight:null,generation:1};
    this.persist(id, entry);
    this.entries.delete(id);
    return this.open(project);
  }
  discard(id) {
    const entry = this.entries.get(id);
    if (entry?.running) throw new Error('Wait for the current save to finish before choosing the saved version.');
    // Preserve a recoverable copy even after the user explicitly chooses the server version.
    if (entry?.latest) this.storage.setItem(this.key(id) + ':previous-draft', JSON.stringify(entry.latest));
    this.storage.removeItem(this.key(id));
    this.cancel(entry?.timer);
    this.entries.delete(id);
  }
}
