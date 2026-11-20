"""Directory ownership uses path components and preserves original precedence."""
from pathlib import PureWindowsPath, PurePosixPath

import pytest

from workbench.backup_references import ReferenceRoots


@pytest.mark.parametrize('path_type,prefix', [(PureWindowsPath, 'C:/books'), (PurePosixPath, '/books')])
def test_root_index_respects_components_and_inventory_priority(path_type, prefix):
    parent = path_type(prefix)
    bases = {'book': parent/'story', 'job': parent/'jobs/job_1', 'nested': parent/'story/assets'}
    index = ReferenceRoots(bases)
    assert index.find(parent/'story') == ('book', bases['book'])
    assert index.find(parent/'story/assets/image.png') == ('book', bases['book'])
    assert index.find(parent/'jobs/job_1/draft.md') == ('job', bases['job'])
    assert index.find(parent/'story-copy/chapter.md') is None
    assert index.find(parent/'jobs/job_10/draft.md') is None
    assert index.find(parent) is None
    assert index.find(path_type('relative/draft.md')) is None
    reverse = ReferenceRoots(dict(reversed(list(bases.items()))))
    assert reverse.find(parent/'story/assets/image.png') == ('nested', bases['nested'])


def test_windows_case_drive_and_unc_boundaries():
    roots = ReferenceRoots({'book': PureWindowsPath('C:/Books/Story'),
        'unc': PureWindowsPath('//server/share/story')})
    assert roots.find(PureWindowsPath('c:/books/STORY/chapter.md'))[0] == 'book'
    assert roots.find(PureWindowsPath('D:/Books/Story/chapter.md')) is None
    assert roots.find(PureWindowsPath('//SERVER/SHARE/story/chapter.md'))[0] == 'unc'
    assert roots.find(PureWindowsPath('//server/other/story/chapter.md')) is None
