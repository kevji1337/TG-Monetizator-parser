import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from monetizer.catalog import DATA_DIR, load_catalog, load_playbook
from monetizer.keywords import MICRO_NICHES, expand_keywords
from monetizer.pitches import compose_pitch, chatgpt_prompt
from monetizer.profile import load_profile, save_profile, profile_version
from monetizer.qualification import qualify
from monetizer.repository import Repository
from monetizer.batch import create_batch
from monetizer.runner import JobRunner, RunGuard
from monetizer.evaluation import review_set, save_labels, validate_labels, evaluate
from test_qualification import expert


class MergeTests(unittest.TestCase):
    def test_stable_ids_and_provenance(self):
        old = json.loads((DATA_DIR / 'sources/keyword_catalog_chatgpt.json').read_text(encoding='utf-8'))
        ids = {m['id'] for n in old['niches'] for m in n['micro_niches']}
        self.assertTrue(ids <= MICRO_NICHES.keys())
        report = json.loads((DATA_DIR / 'merge_report.json').read_text(encoding='utf-8'))
        self.assertEqual(len(report['source_hashes']), 4)
        self.assertTrue(report['pending_review'])

    def test_merge_is_reproducible(self):
        from scripts import merge_catalogs
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            shutil.copytree(DATA_DIR / 'sources', data / 'sources')
            with patch.object(merge_catalogs, 'DATA', data), redirect_stdout(io.StringIO()):
                merge_catalogs.merge()
                first = {p.name: p.read_bytes() for p in data.glob('*.json')}
                merge_catalogs.merge()
            self.assertEqual(first, {p.name: p.read_bytes() for p in data.glob('*.json')})
            self.assertEqual(load_catalog(data / 'keyword_catalog.json'), load_catalog())
            self.assertEqual(load_playbook(data / 'outreach_playbook.json'), load_playbook())

    def test_split_ai_directions(self):
        dev = expand_keywords(niche_id='2', micro_niche_id='ai_for_developers', limit=50)
        analyst = expand_keywords(niche_id='2', micro_niche_id='ai_for_analysts', limit=50)
        self.assertIn('LangChain', dev)
        self.assertNotIn('SQL с нейросетью', dev)
        self.assertIn('SQL с нейросетью', analyst)
        self.assertNotIn('Claude Code', analyst)

    def test_string_example_rejected_cleanly(self):
        with tempfile.TemporaryDirectory() as folder:
            data = copy.deepcopy(load_playbook())
            data['scenarios'][0]['examples'][0]['input_evidence'] = 'fictional example'
            path = Path(folder) / 'playbook.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_playbook(path)


class EvidenceTests(unittest.TestCase):
    def test_duplicate_post_not_two_independent_proofs(self):
        row = expert()
        row['about_text'] = ''
        row['content_samples'][1]['text'] = row['content_samples'][0]['text']
        result = qualify(row, micro_id='motion_design')
        self.assertEqual(result['channel_type'], 'unknown')
        self.assertEqual(len(result['classification']['positive_evidence']), 1)

    def test_generic_identity_not_expertise(self):
        row = expert()
        row['about_text'] = 'Меня зовут Анна. Добро пожаловать.'
        row['content_samples'] = [{'id': 'one', 'text': 'Показываю прогулку.'}, {'id': 'two', 'text': 'Объясняю правила чата.'}]
        self.assertEqual(qualify(row, micro_id='motion_design')['channel_type'], 'unknown')

    def test_late_match_visible_in_literal_quote(self):
        row = expert()
        row['about_text'] = ''
        row['content_samples'][0]['text'] = 'Вступление. ' * 60 + 'Разбираю After Effects и Lottie на своём проекте.'
        result = qualify(row, micro_id='motion_design')
        proofs = result['classification']['relevance_evidence']
        lottie = next(p for p in proofs if p['term'].casefold() == 'lottie')
        self.assertIn('Lottie', lottie['quote'])
        self.assertIn(lottie['quote'], row['content_samples'][0]['text'])

    def test_one_tool_overlapping_aliases_not_two_indicators(self):
        row = expert()
        row['about_text'] = 'Я дизайнер. After Effects.'
        row['content_samples'] = [{'id': 'first', 'text': 'Разбираю After Effects.'},
                                  {'id': 'second', 'text': 'Показываю After Effects.'}]
        result = qualify(row, micro_id='motion_design')
        self.assertEqual(result['relevance'], 'unknown')
        self.assertEqual(result['status'], 'review')


class ProfileAndQualityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.directory.name) / 'db.sqlite3')
        self.row = qualify(expert(), micro_id='motion_design')
        self.repo.save_state({'run_id': 'test', 'rows': [self.row], 'niche': 'Моушн', 'settings': {}})

    def tearDown(self):
        self.directory.cleanup()

    def labelled(self, qualified=True):
        packet = review_set(self.repo)
        packet['channels'][0]['label'] = {'channel_type': 'expert', 'relevance': 'matched', 'qualified': qualified,
                                         'annotator': 'Ручной проверяющий', 'note': 'Описание и два собственных проекта.'}
        return packet

    def test_profile_persistence(self):
        profile = save_profile(self.repo, {'name': 'Алексей', 'role': 'маркетолог', 'services': 'привлечение заявок',
                                           'verified_facts': 'Опыт задан пользователем', 'terms': 'Условия обсуждаются отдельно'})
        self.assertEqual(profile, load_profile(Repository(self.repo.path)))
        self.assertNotEqual(profile_version(profile), profile_version({}))

    def test_profile_validation_atomic(self):
        original = load_profile(self.repo)
        with self.assertRaises(ValueError):
            save_profile(self.repo, {'name': ['invalid']})
        self.assertEqual(original, load_profile(self.repo))

    def test_profile_in_single_and_batch_prompts(self):
        profile = save_profile(self.repo, {'name': 'Алексей', 'services': 'привлечение заявок'})
        self.assertIn('Алексей', chatgpt_prompt(self.row, profile=profile))
        self.assertEqual(create_batch(self.repo, 'test')['sender_profile'], profile)

    def test_no_invented_sender_or_gender_claim(self):
        draft = compose_pitch(self.row)
        self.assertNotIn('Я — партнёр', draft['message'])
        self.assertNotIn('Нашёл', draft['message'])
        self.assertTrue(any('профиль' in w for w in draft['warnings']))

    def test_existing_consultation_and_profile(self):
        self.row['content_samples'][0]['text'] = 'Провожу консультации по анимации. Запись ко мне.'
        draft = compose_pitch(self.row, profile={'name': 'Алексей', 'role': 'маркетолог', 'services': 'реклама'})
        self.assertEqual(draft['scenario_id'], 'consultations_explicit')
        self.assertIn('на ваши консультации', draft['message'])
        self.assertIn('Алексей', draft['message'])

    def test_free_material_scenario_requires_review(self):
        source = self.row['content_samples'][0]
        source['text'] = 'Мой шаблон презентации можно скачать по ссылке.'
        with self.assertRaises(ValueError):
            compose_pitch(self.row, scenario_id='free_materials_lead_magnet', evidence_id=source['id'])
        result = compose_pitch(self.row, scenario_id='free_materials_lead_magnet', evidence_id=source['id'], confirmed=True)
        self.assertIn('шаблон', result['message'])

    def test_no_metrics_without_human_labels(self):
        report = evaluate(self.repo)
        self.assertEqual(report['labelled'], 0)
        self.assertIsNone(report['precision'])
        self.assertIsNone(report['recall'])

    def test_labels_survive_restart_and_import_is_idempotent(self):
        packet = self.labelled()
        self.assertEqual(save_labels(self.repo, packet), 1)
        save_labels(self.repo, packet)
        report = evaluate(Repository(self.repo.path))
        self.assertEqual(report['labelled'], 1)
        self.assertEqual(report['tp'], 1)
        self.assertEqual(report['precision'], 1)
        self.assertEqual(report['errors'], [])
        self.assertEqual(self.repo.list_channels()[0]['work_status'], 'new')

    def test_false_positive_is_reported(self):
        save_labels(self.repo, self.labelled(False))
        report = evaluate(self.repo)
        self.assertEqual(report['fp'], 1)
        self.assertEqual(report['precision'], 0)
        self.assertEqual(len(report['errors']), 1)

    def test_bad_labels_do_not_write_anything(self):
        packet = self.labelled()
        bad = copy.deepcopy(packet['channels'][0])
        bad['channel_id'] = 'foreign_channel'
        packet['channels'].append(bad)
        with self.assertRaises(ValueError):
            save_labels(self.repo, packet)
        self.assertEqual(evaluate(self.repo)['labelled'], 0)

    def test_stale_hash_rejected(self):
        packet = self.labelled()
        packet['channels'][0]['content_hash'] = 'outdated'
        with self.assertRaises(ValueError):
            validate_labels(self.repo, packet)

    def test_stale_labels_excluded(self):
        save_labels(self.repo, self.labelled())
        self.row['about_text'] += ' Новые сведения.'
        self.repo.save_state({'run_id': 'test', 'rows': [self.row], 'settings': {}})
        self.assertEqual(evaluate(self.repo)['labelled'], 0)
        self.assertEqual(evaluate(self.repo)['stale_labels'], 1)

    def test_unlabelled_partial_response_is_allowed(self):
        packet = review_set(self.repo)
        self.assertEqual(validate_labels(self.repo, packet), [])

    def test_unknown_not_positive(self):
        packet = self.labelled()
        packet['channels'][0]['label']['channel_type'] = 'unknown'
        with self.assertRaises(ValueError):
            save_labels(self.repo, packet)

    def test_guard_is_specific_to_database(self):
        with RunGuard(self.repo.path):
            with self.assertRaises(RuntimeError):
                with RunGuard(self.repo.path):
                    pass
            other = Path(self.directory.name) / 'other/db.sqlite3'
            with RunGuard(other):
                pass

    def test_runner_temporary_database_is_independent(self):
        with RunGuard(self.repo.path):
            runner = JobRunner(Path(self.directory.name) / 'independent/checkpoint.json')
            self.assertFalse(runner.snapshot()['external_owner'])
