import unittest

from record_reader import read_records


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.tables = read_records()

    def references(self, left, left_key, right, right_key):
        candidates = {row[right_key] for row in self.tables[right]}
        self.assertTrue(all(row[left_key] in candidates for row in self.tables[left]))

    def test_documents_reference_source_records(self):
        self.references('submissions','claim','claims','claim')
        self.references('citations','version','versions','version')
        self.references('citations','submission','submissions','submission')
        self.references('feedback','submission','submissions','submission')
        self.assertTrue(any(first['url']==other['url'] and first['evidence']!=other['evidence']
            for first in self.tables['versions'] for other in self.tables['versions']))
        transitions = [row for row in self.tables['relations'] if row['dispute']=='d2']
        self.assertEqual([row['state'] for row in transitions], ['pending','confirmed'])
        self.assertTrue(any(row['locked_dispute'] for row in self.tables['review_slots']))
