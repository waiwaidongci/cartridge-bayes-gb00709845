import unittest

from src.services.domain import compute, sample_rows
from src.services.merge import (business_content, correction_records, merge_records,
                                revisioned_content)

NOW = 1_763_980_800.0


def row(cart, rev, label='退件', cohort='训练', streak=10.0, remaining=50.0, density=5.0,
        school='澄江一小', day='2026-11-02'):
    return {'cartridge_id': cart, 'school': school, 'inspected_on': day, 'cohort': cohort,
            'verified_label': label, 'remaining_pct': remaining, 'streak_pct': streak,
            'density_error_pct': density, 'revision': rev}


def csv_text(rows, header=('cartridge_id', 'school', 'inspected_on', 'cohort', 'verified_label',
                           'remaining_pct', 'streak_pct', 'density_error_pct', 'revision')):
    lines = [','.join(header)]
    for values in rows:
        lines.append(','.join(str(value) for value in values))
    return '\n'.join(lines) + '\n'


class CorrectionParseTest(unittest.TestCase):
    def test_parse_with_revision_column(self):
        rows = correction_records(csv_text([('CJ-1', '澄江一小', '2026-11-02', '训练', '退件', 50, 10, 5, 3)]))
        self.assertEqual(rows[0]['revision'], 3)
        self.assertIs(type(rows[0]['revision']), int)

    def test_header_must_include_revision(self):
        text = csv_text([('CJ-1', '澄江一小', '2026-11-02', '训练', '退件', 50, 10, 5)],
                        header=('cartridge_id', 'school', 'inspected_on', 'cohort', 'verified_label',
                                'remaining_pct', 'streak_pct', 'density_error_pct'))
        with self.assertRaisesRegex(ValueError, 'revision'):
            correction_records(text)

    def test_revision_must_be_integer(self):
        with self.assertRaisesRegex(ValueError, '校点修订号'):
            correction_records(csv_text([('CJ-1', '澄江一小', '2026-11-02', '训练', '退件', 50, 10, 5, '1.5')]))

    def test_revision_must_be_within_range(self):
        with self.assertRaisesRegex(ValueError, '校点修订号'):
            correction_records(csv_text([('CJ-1', '澄江一小', '2026-11-02', '训练', '退件', 50, 10, 5, -1)]))


class MergeTest(unittest.TestCase):
    def test_out_of_order_higher_revision_wins(self):
        base = [row('CJ-1', 1, streak=10.0)]
        incoming = [row('CJ-1', 3, streak=30.0), row('CJ-1', 2, streak=20.0)]
        merged, quarantined, events = merge_records(base, {}, incoming, NOW)
        self.assertEqual(merged[0]['streak_pct'], 30.0)
        self.assertEqual(merged[0]['revision'], 3)
        self.assertEqual(events['applied'], ['CJ-1'])
        self.assertEqual(quarantined, {})

    def test_stale_revision_never_overwrites(self):
        base = [row('CJ-1', 2, streak=20.0)]
        incoming = [row('CJ-1', 1, streak=99.0), row('CJ-1', 0, streak=88.0)]
        merged, _, events = merge_records(base, {}, incoming, NOW)
        self.assertEqual(merged[0]['streak_pct'], 20.0)
        self.assertEqual(merged[0]['revision'], 2)
        self.assertEqual(events['stale'], ['CJ-1'])

    def test_same_revision_conflict_quarantines_only_that_item(self):
        base = [row('CJ-1', 1, streak=10.0), row('CJ-2', 1, streak=40.0)]
        incoming = [row('CJ-1', 1, streak=99.0), row('CJ-2', 2, streak=45.0)]
        merged, quarantined, events = merge_records(base, {}, incoming, NOW)
        self.assertEqual([item['cartridge_id'] for item in merged], ['CJ-2'])
        self.assertEqual(merged[0]['streak_pct'], 45.0)
        self.assertEqual(events['quarantined'], ['CJ-1'])
        self.assertEqual(events['applied'], ['CJ-2'])
        self.assertEqual(quarantined['CJ-1']['revision'], 1)
        self.assertEqual(len(quarantined['CJ-1']['contents']), 2)

    def test_higher_revision_lifts_quarantine(self):
        base = [row('CJ-1', 1, streak=10.0)]
        _, quarantined, _ = merge_records(base, {}, [row('CJ-1', 1, streak=99.0)], NOW)
        self.assertIn('CJ-1', quarantined)
        merged, quarantined, events = merge_records(base, quarantined, [row('CJ-1', 2, streak=55.0)], NOW + 60)
        self.assertEqual(quarantined, {})
        self.assertEqual(events['released'], ['CJ-1'])
        self.assertEqual(merged[0]['streak_pct'], 55.0)
        self.assertEqual(merged[0]['revision'], 2)

    def test_same_revision_same_content_is_idempotent(self):
        base = [row('CJ-1', 1, streak=10.0)]
        merged, quarantined, events = merge_records(base, {}, [row('CJ-1', 1, streak=10.0)], NOW)
        self.assertEqual(events['duplicates'], ['CJ-1'])
        self.assertEqual(quarantined, {})
        self.assertEqual(business_content(merged), business_content(base))

    def test_same_revision_does_not_lift_quarantine(self):
        quarantined = {'CJ-1': {'revision': 2, 'at': NOW,
                                'contents': [row('CJ-1', 2, streak=10.0), row('CJ-1', 2, streak=20.0)]}}
        merged, held, events = merge_records([], quarantined, [row('CJ-1', 2, streak=30.0)], NOW + 60)
        self.assertEqual(merged, [])
        self.assertEqual(events['held'], ['CJ-1'])
        self.assertEqual(len(held['CJ-1']['contents']), 3)

    def test_conflict_within_incoming_batch_quarantines(self):
        merged, quarantined, events = merge_records([], {}, [row('CJ-1', 1, streak=10.0),
                                                             row('CJ-1', 1, streak=20.0)], NOW)
        self.assertEqual(merged, [])
        self.assertEqual(events['quarantined'], ['CJ-1'])
        self.assertEqual(len(quarantined['CJ-1']['contents']), 2)

    def test_stale_relative_to_quarantine_is_ignored(self):
        quarantined = {'CJ-1': {'revision': 3, 'at': NOW, 'contents': [row('CJ-1', 3, streak=10.0)]}}
        merged, held, events = merge_records([], quarantined, [row('CJ-1', 2, streak=99.0)], NOW + 60)
        self.assertEqual(merged, [])
        self.assertEqual(events['stale'], ['CJ-1'])
        self.assertEqual(held['CJ-1']['revision'], 3)

    def test_correction_can_add_missing_cartridge(self):
        merged, _, events = merge_records([], {}, [row('CJ-9', 0, streak=12.0)], NOW)
        self.assertEqual([item['cartridge_id'] for item in merged], ['CJ-9'])
        self.assertEqual(events['applied'], ['CJ-9'])

    def test_business_content_ignores_revision_metadata(self):
        base = [row('CJ-1', 0, streak=10.0)]
        merged, _, _ = merge_records(base, {}, [row('CJ-1', 0, streak=10.0)], NOW)
        self.assertEqual(business_content(base), business_content(merged))
        changed, _, _ = merge_records(base, {}, [row('CJ-1', 1, streak=11.0)], NOW)
        self.assertNotEqual(business_content(base), business_content(changed))

    def test_revision_bump_persists_and_enables_future_conflict_detection(self):
        base = [row('CJ-1', 0, streak=10.0)]
        merged, quarantined, _ = merge_records(base, {}, [row('CJ-1', 1, streak=10.0)], NOW)
        self.assertEqual(merged[0]['revision'], 1)
        self.assertEqual(business_content(base), business_content(merged))  # 内容未变，不必重算
        self.assertNotEqual(revisioned_content(base), revisioned_content(merged))  # 但须保存版本
        remerged, quarantined, events = merge_records(merged, quarantined,
                                                      [row('CJ-1', 1, streak=99.0)], NOW + 60)
        self.assertEqual(events['quarantined'], ['CJ-1'])  # 同版不同内容：隔离
        self.assertEqual(remerged, [])


class MergedComputeTest(unittest.TestCase):
    def test_compute_runs_on_merged_records_and_excludes_quarantined(self):
        base = sample_rows()
        target = next(item for item in base if item['cohort'] == '候选')
        conflicting = {**target, 'streak_pct': target['streak_pct'] + 1, 'revision': 0}
        merged, quarantined, _ = merge_records(base, {}, [conflicting], NOW)
        self.assertIn(target['cartridge_id'], quarantined)
        report = compute(merged, {'false_return_cost': 35, 'missed_return_cost': 115})
        self.assertEqual(report['metrics']['候选件数'], 11)
        self.assertEqual(report['metrics']['训练件数'], 80)
        self.assertNotIn(target['cartridge_id'], [item['耗材编号'] for item in report['rows']])

    def test_corrected_holdout_values_change_recalculation(self):
        base = sample_rows()
        holdout = next(item for item in base if item['cohort'] == '留出' and item['verified_label'] == '留用')
        corrected = {**holdout, 'revision': 1, 'remaining_pct': 100.0, 'streak_pct': 100.0,
                     'density_error_pct': 100.0}
        merged, _, events = merge_records(base, {}, [corrected], NOW)
        self.assertEqual(events['applied'], [holdout['cartridge_id']])
        before = compute(base, {'false_return_cost': 35, 'missed_return_cost': 115})
        after = compute(merged, {'false_return_cost': 35, 'missed_return_cost': 115})
        self.assertNotEqual(before['metrics']['成本阈值错判成本（元）'], after['metrics']['成本阈值错判成本（元）'])


if __name__ == '__main__':
    unittest.main()
