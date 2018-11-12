import hashlib
import os
import sys
from functools import wraps
from io import open

from pony.orm import Database, Required, db_session, Set, Optional

from mutmut import BAD_TIMEOUT, OK_SUSPICIOUS, BAD_SURVIVED, get_apply_line, UNTESTED, OK_KILLED

if sys.version_info < (3, 0):   # pragma: no cover (python 2 specific)
    # noinspection PyUnresolvedReferences
    text_type = unicode
else:
    text_type = str


db = Database()


class SourceFile(db.Entity):
    filename = Required(str)
    lines = Set('Line')


class Line(db.Entity):
    sourcefile = Required(SourceFile)
    line = Required(text_type)
    mutants = Set('Mutant')


class Mutant(db.Entity):
    line = Required(Line)
    index = Required(int)
    tested_against_hash = Optional(text_type)
    status = Required(text_type)  # really an enum of mutant_statuses


def init_db(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if db.provider is None:
            db.bind(provider='sqlite', filename=os.path.join(os.getcwd(), '.mutmut-cache'), create_db=True)
            db.generate_mapping(create_tables=True)

        return f(*args, **kwargs)
    return wrapper


def hash_of(filename):
    with open(filename, 'rb') as f:
        m = hashlib.sha256()
        m.update(f.read())
        return m.hexdigest()


def hash_of_tests(tests_dirs):
    m = hashlib.sha256()
    for tests_dir in tests_dirs:
        for root, dirs, files in os.walk(tests_dir):
            for filename in files:
                with open(os.path.join(root, filename), 'rb') as f:
                    m.update(f.read())
    return m.hexdigest()


def enumerate_mutants():
    for sourcefile in SourceFile.select():
        for line in sourcefile.lines:
            for mutant in line.mutants:
                yield mutant


@init_db
@db_session
def print_result_cache():
    print('Timed out ⏰')
    for mutant in enumerate_mutants():
        if mutant.status == BAD_TIMEOUT:
            print(get_apply_line(mutant.line.sourcefile.filename, (mutant.line.line, mutant.index)))

    print()
    print('Suspicious 🤔')
    for mutant in enumerate_mutants():
        if mutant.status == OK_SUSPICIOUS:
            print(get_apply_line(mutant.line.sourcefile.filename, (mutant.line.line, mutant.index)))

    print()
    print('Survived 🙁')
    for mutant in enumerate_mutants():
        if mutant.status == BAD_SURVIVED:
            print(get_apply_line(mutant.line.sourcefile.filename, (mutant.line.line, mutant.index)))


def get_or_create(model, defaults=None, **params):
    if defaults is None:
        defaults = {}
    obj = model.get(**params)
    if obj is None:
        params = params.copy()
        for k, v in defaults.items():
            if k not in params:
                params[k] = v
        return model(**params)
    else:
        return obj


@init_db
@db_session
def register_mutant(filename, mutation_id):
    sourcefile = get_or_create(SourceFile, filename=filename)

    line = get_or_create(Line, sourcefile=sourcefile, line=mutation_id[0])
    get_or_create(Mutant, line=line, index=mutation_id[1], defaults=dict(status='unknown'))


@init_db
@db_session
def update_mutant_status(file_to_mutate, mutation_id, status, tests_hash):
    sourcefile = SourceFile.get(filename=file_to_mutate)
    line = Line.get(sourcefile=sourcefile, line=mutation_id[0])
    mutant = Mutant.get(line=line, index=mutation_id[1])
    mutant.status = status
    mutant.tested_against_hash = tests_hash


@init_db
@db_session
def cached_mutation_status(filename, mutation_id, hash_of_tests):
    sourcefile = SourceFile.get(filename=filename)
    line = Line.get(sourcefile=sourcefile, line=mutation_id[0])
    mutant = Mutant.get(line=line, index=mutation_id[1])

    if mutant.status == OK_KILLED:
        # We assume that if a mutant was killed, a change to the test suite will mean it's still killed
        return OK_KILLED

    if mutant.tested_against_hash != hash_of_tests:
        return UNTESTED

    return mutant.status