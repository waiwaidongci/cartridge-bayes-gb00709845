import assert from 'node:assert/strict';
import test from 'node:test';

import {freshness, pickCurrent} from '../src/utils/runs.js';

test('当前有效结果为已完成的最高编号任务', () => {
  const runs = [
    {id: 7, state: 'queued'},
    {id: 6, state: 'completed'},
    {id: 5, state: 'completed'},
  ];
  assert.equal(pickCurrent(runs).id, 6);
});

test('较早一批晚算完也不顶替新版', () => {
  // 第 6 批先算完成为当前；第 5 批随后才算完，当前仍为第 6 批
  const before = [{id: 6, state: 'completed'}, {id: 5, state: 'running'}];
  assert.equal(pickCurrent(before).id, 6);
  const after = [{id: 6, state: 'completed'}, {id: 5, state: 'completed'}];
  assert.equal(pickCurrent(after).id, 6);
});

test('重算失败时保留旧版并报告失败批次', () => {
  const runs = [
    {id: 8, state: 'failed', error: '留出组至少需要 1 条已确认退件的记录'},
    {id: 6, state: 'completed'},
  ];
  assert.equal(pickCurrent(runs).id, 6);
  const {pending, failed} = freshness(runs, 6);
  assert.equal(pending, false);
  assert.equal(failed.id, 8);
});

test('新一批排队或计算中时标记为待切换', () => {
  const runs = [{id: 9, state: 'running'}, {id: 8, state: 'failed'}, {id: 6, state: 'completed'}];
  const {pending, failed} = freshness(runs, 6);
  assert.equal(pending, true);
  assert.equal(failed.id, 8);
});

test('没有更新批次时不待切换也不报错', () => {
  const runs = [{id: 6, state: 'completed'}, {id: 5, state: 'completed'}];
  const {pending, failed} = freshness(runs, 6);
  assert.equal(pending, false);
  assert.equal(failed, null);
});

test('没有已完成任务时返回空', () => {
  assert.equal(pickCurrent([{id: 1, state: 'queued'}]), null);
});
