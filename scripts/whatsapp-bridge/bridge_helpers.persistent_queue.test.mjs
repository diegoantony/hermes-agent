/**
 * Unit tests for createPersistentQueueStore: the disk-backed append-only
 * JSONL store that lets messageQueue survive a process death (t_df8f51db,
 * follow-up to t_b9ee59e4 — a clean process restart with items still
 * queued in memory used to lose them with zero trace).
 *
 * These tests avoid importing bridge.js because that file starts an HTTP
 * server and Baileys socket at module load. Keep the helper module pure.
 */

import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync, readFileSync, existsSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { createPersistentQueueStore, recoverPendingQueue } from './bridge_helpers.js';

function withTempDir(fn) {
  const dir = mkdtempSync(path.join(tmpdir(), 'wa-pending-queue-'));
  try {
    fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

// -- append + loadPending round trip --------------------------------------
withTempDir((dir) => {
  const filePath = path.join(dir, 'nested', 'pending_queue.jsonl');
  const store = createPersistentQueueStore(filePath);

  assert.deepEqual(store.loadPending(), [], 'missing file loads as empty');

  const eventA = { messageId: 'a', chatId: '1@s.whatsapp.net', body: 'first' };
  const eventB = { messageId: 'b', chatId: '1@s.whatsapp.net', body: 'second' };
  store.append(eventA);
  store.append(eventB);

  assert.ok(existsSync(filePath), 'append creates parent dirs and the file');
  const loaded = store.loadPending();
  assert.deepEqual(loaded, [eventA, eventB], 'loadPending replays events in append order');
  console.log('  ✓ append + loadPending round-trips events, creating nested dirs as needed');
});

// -- truncate clears the backing file --------------------------------------
withTempDir((dir) => {
  const filePath = path.join(dir, 'pending_queue.jsonl');
  const store = createPersistentQueueStore(filePath);
  store.append({ messageId: 'a' });
  store.append({ messageId: 'b' });
  assert.equal(store.loadPending().length, 2);

  store.truncate();
  assert.deepEqual(store.loadPending(), [], 'truncate empties the file');
  assert.equal(readFileSync(filePath, 'utf8'), '', 'file content is exactly empty after truncate');
  console.log('  ✓ truncate clears all previously-appended events');
});

// -- corrupt/partial lines are skipped, not fatal --------------------------
withTempDir((dir) => {
  const filePath = path.join(dir, 'pending_queue.jsonl');
  const warnings = [];
  const store = createPersistentQueueStore(filePath, { log: (msg) => warnings.push(msg) });

  store.append({ messageId: 'good-1' });
  // Simulate a hard-kill that flushed a trailing newline but left the JSON
  // itself truncated (OS write boundaries don't always line up with the
  // JSON.stringify() byte count on a mid-write kill).
  writeFileSync(filePath, '{"messageId": "trunc\n', { flag: 'a' });
  store.append({ messageId: 'good-2' });

  const loaded = store.loadPending();
  assert.deepEqual(loaded.map(e => e.messageId), ['good-1', 'good-2'], 'corrupt line is skipped, good lines survive');
  assert.ok(warnings.some(w => w.includes('corrupt')), 'a warning is logged for the corrupt line');
  console.log('  ✓ a corrupt/partial line (own trailing newline) is skipped instead of throwing');
});

// -- recoverPendingQueue compacts away a dangling no-newline partial write -
withTempDir((dir) => {
  const filePath = path.join(dir, 'pending_queue.jsonl');
  const store = createPersistentQueueStore(filePath);

  store.append({ messageId: 'good-1' });
  // Worst case: the kill happened mid fs.appendFileSync() itself, so the
  // file ends with NO trailing newline at all. Without compaction, the next
  // raw append() would concatenate straight onto this and corrupt itself too.
  writeFileSync(filePath, '{"messageId": "dangling-no-newline"', { flag: 'a' });

  const recovered = recoverPendingQueue(store);
  assert.deepEqual(recovered.map(e => e.messageId), ['good-1'], 'recovery discards the dangling partial line');

  // Now simulate a brand-new event arriving right after boot. If the file
  // hadn't been compacted, this append would merge with the leftover bytes.
  store.append({ messageId: 'good-2' });
  const loaded = store.loadPending();
  assert.deepEqual(loaded.map(e => e.messageId), ['good-1', 'good-2'], 'post-recovery appends stay well-formed');
  console.log('  ✓ recoverPendingQueue compacts the file so a dangling no-newline write cannot corrupt the next append');
});

// -- recovery is capped so a runaway file can't OOM the process -----------
withTempDir((dir) => {
  const filePath = path.join(dir, 'pending_queue.jsonl');
  const store = createPersistentQueueStore(filePath, { maxRecoveredEvents: 3 });
  for (let i = 0; i < 10; i += 1) {
    store.append({ messageId: `m${i}` });
  }
  const loaded = store.loadPending();
  assert.equal(loaded.length, 3, 'recovery is capped at maxRecoveredEvents');
  assert.deepEqual(loaded.map(e => e.messageId), ['m7', 'm8', 'm9'], 'cap keeps the newest events, not the oldest');
  console.log('  ✓ recovery caps event count and keeps the newest events when the file is huge');
});

// -- append/truncate failures are swallowed, not thrown --------------------
{
  const warnings = [];
  const store = createPersistentQueueStore('/nonexistent-root-only-dir/pending_queue.jsonl', {
    log: (msg) => warnings.push(msg),
    mkdirSyncFn: () => { throw new Error('EACCES: permission denied'); },
  });
  assert.doesNotThrow(() => store.append({ messageId: 'x' }), 'append never throws even if the write fails');
  assert.doesNotThrow(() => store.truncate(), 'truncate never throws even if the write fails');
  assert.ok(warnings.length >= 2, 'both failures are logged');
  console.log('  ✓ append/truncate failures are logged, never thrown (must not crash the bridge)');
}

console.log('bridge_helpers.persistent_queue.test.mjs: all assertions passed');
