// 当前有效结果的选择规则：已完成的任务中编号最大者。
// 任务编号随提交批次递增，因此较早一批即使晚算完，也不会顶替较新的批次。

export function pickCurrent(runs) {
  return runs.find(run => run.state === 'completed') || null;
}

// 相对当前有效结果，更新批次的状态：是否仍在排队/计算，或最新一批已失败。
export function freshness(runs, currentId) {
  const newer = runs.filter(run => run.id > currentId);
  return {
    pending: newer.some(run => run.state === 'queued' || run.state === 'running'),
    failed: newer.find(run => run.state === 'failed') || null,
  };
}
