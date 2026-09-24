import pytest
from django.core.validators import MinLengthValidator

from rest_framework import serializers


class AuthorInstance:
    def __init__(self, email_value, secret):
        self.email_value = email_value
        self.secret = secret
        self.email_accesses = 0
        self.secret_accesses = 0

    @property
    def email(self):
        self.email_accesses += 1
        return self.email_value

    @property
    def private_secret(self):
        self.secret_accesses += 1
        return self.secret


class AuthorSerializer(serializers.Serializer):
    email = serializers.CharField()
    secret = serializers.CharField(source='private_secret')


class CommentInstance:
    def __init__(self, owner=None, text_value='comment'):
        self.owner = owner
        self.text_value = text_value
        self.text_accesses = 0

    @property
    def text(self):
        self.text_accesses += 1
        return self.text_value


class CommentSerializer(serializers.Serializer):
    text = serializers.CharField()
    author = AuthorSerializer(source='owner')


class BookInstance:
    def __init__(self, name, writer=None, remarks=None, omitted='unused'):
        self.name = name
        self.writer_value = writer
        self.remarks_value = remarks
        self.omitted_value = omitted
        self.author_accesses = 0
        self.omitted_accesses = 0
        self.summary_calls = 0

    @property
    def writer(self):
        self.author_accesses += 1
        return self.writer_value

    @property
    def remarks(self):
        return self.remarks_value

    @property
    def omitted(self):
        self.omitted_accesses += 1
        return self.omitted_value


class BookSerializer(serializers.Serializer):
    name = serializers.CharField()
    author = AuthorSerializer(source='writer')
    comments = CommentSerializer(many=True, allow_null=True, source='remarks')
    summary = serializers.SerializerMethodField()
    omitted = serializers.CharField()

    def get_summary(self, instance):
        instance.summary_calls += 1
        return f'summary:{instance.name}'


@pytest.fixture
def book():
    return BookInstance(
        name='Book',
        writer=AuthorInstance('a@example.com', 'secret'),
        remarks=[
            CommentInstance(AuthorInstance('c@example.com', 'comment-secret')),
        ],
    )


def test_projects_fields_and_avoids_excluded_attribute_and_method_access(book):
    serializer = BookSerializer(
        book,
        fields=['name', 'author.email', 'comments.author.email'],
    )

    assert serializer.data == {
        'name': 'Book',
        'author': {'email': 'a@example.com'},
        'comments': [{'author': {'email': 'c@example.com'}}],
    }
    assert book.author_accesses == 1
    assert book.omitted_accesses == 0
    assert book.summary_calls == 0
    assert book.writer_value.email_accesses == 1
    assert book.writer_value.secret_accesses == 0
    comment = book.remarks_value[0]
    assert comment.text_accesses == 0
    assert comment.owner.email_accesses == 1
    assert comment.owner.secret_accesses == 0


def test_method_field_is_called_only_when_selected(book):
    serializer = BookSerializer(book, fields=['summary'])

    assert serializer.data == {'summary': 'summary:Book'}
    assert book.summary_calls == 1
    assert book.author_accesses == 0
    assert book.omitted_accesses == 0


def test_nested_many_projection_handles_null_and_empty_list():
    null_book = BookInstance(name='Null', remarks=None)
    empty_book = BookInstance(name='Empty', remarks=[])

    null_serializer = BookSerializer(
        null_book,
        fields=['name', 'comments.author.email'],
    )
    empty_serializer = BookSerializer(
        empty_book,
        fields=['name', 'comments.author.email'],
    )

    assert null_serializer.data == {'name': 'Null', 'comments': None}
    assert empty_serializer.data == {'name': 'Empty', 'comments': []}


def test_two_projections_of_same_serializer_are_independent(book):
    name_only = BookSerializer(book, fields=['name'])
    author_only = BookSerializer(book, fields=['author.secret'])

    assert name_only.data == {'name': 'Book'}
    assert author_only.data == {'author': {'secret': 'secret'}}
    assert book.summary_calls == 0
    assert book.omitted_accesses == 0
    assert book.writer_value.secret_accesses == 1
    assert book.writer_value.email_accesses == 0


def test_parent_and_child_path_includes_complete_parent(book):
    serializer = BookSerializer(book, fields=['author', 'author.email'])

    assert serializer.data == {
        'author': {'email': 'a@example.com', 'secret': 'secret'}
    }
    assert book.writer_value.secret_accesses == 1


def test_reverse_parent_and_child_path_order_has_same_result(book):
    serializer = BookSerializer(book, fields=['author.email', 'author'])

    assert serializer.data == {
        'author': {'email': 'a@example.com', 'secret': 'secret'}
    }
    assert book.writer_value.secret_accesses == 1


def test_unknown_fields_fail_before_serialization(book):
    with pytest.raises(ValueError, match='Unknown field `missing`'):
        BookSerializer(book, fields=['missing'])

    with pytest.raises(ValueError, match='Unknown field `private_secret`'):
        BookSerializer(book, fields=['author.private_secret'])

    with pytest.raises(ValueError, match='Unknown field `email_value`'):
        BookSerializer(book, fields=['author.email_value'])

    assert book.author_accesses == 0
    assert book.summary_calls == 0


def test_full_serialization_without_fields_is_unchanged(book):
    assert BookSerializer(book).data == {
        'name': 'Book',
        'author': {'email': 'a@example.com', 'secret': 'secret'},
        'comments': [
            {
                'text': 'comment',
                'author': {'email': 'c@example.com', 'secret': 'comment-secret'},
            }
        ],
        'summary': 'summary:Book',
        'omitted': 'unused',
    }


def test_top_level_many_uses_item_serializer_projection(book):
    other = BookInstance(name='Other')
    serializer = BookSerializer([book, other], many=True, fields=['name'])

    assert serializer.data == [{'name': 'Book'}, {'name': 'Other'}]
    assert book.author_accesses == 0
    assert book.summary_calls == 0


def test_explicit_list_serializer_child_projection_is_preserved(book):
    child = BookSerializer(fields=['name'])
    serializer = serializers.ListSerializer(child=child, instance=[book])

    assert serializer.data == [{'name': 'Book'}]
    assert book.author_accesses == 0
    assert book.summary_calls == 0


def test_same_serializer_class_can_have_different_projections():
    class PersonSerializer(serializers.Serializer):
        email = serializers.CharField()
        secret = serializers.CharField()

    class RootSerializer(serializers.Serializer):
        primary = PersonSerializer()
        backup = PersonSerializer()

    instance = {
        'primary': {'email': 'primary@example.com', 'secret': 'one'},
        'backup': {'email': 'backup@example.com', 'secret': 'two'},
    }

    serializer = RootSerializer(
        instance,
        fields=['primary.email', 'backup.secret'],
    )

    assert serializer.data == {
        'primary': {'email': 'primary@example.com'},
        'backup': {'secret': 'two'},
    }


class TestSourceStarProjection:
    def test_source_star_uses_public_nested_paths(self):
        class NestedSerializer(serializers.Serializer):
            a = serializers.IntegerField()
            b = serializers.IntegerField()

        class RootSerializer(serializers.Serializer):
            nested = NestedSerializer(source='*')
            other = NestedSerializer(source='*')

        instance = {'a': 1, 'b': 2}

        assert RootSerializer(instance, fields=['nested.a']).data == {
            'nested': {'a': 1}
        }

    def test_source_star_siblings_have_independent_projections(self):
        class NestedSerializer(serializers.Serializer):
            a = serializers.IntegerField()
            b = serializers.IntegerField()

        class RootSerializer(serializers.Serializer):
            first = NestedSerializer(source='*')
            second = NestedSerializer(source='*')

        instance = {'a': 1, 'b': 2}

        assert RootSerializer(
            instance,
            fields=['first.a', 'second.b'],
        ).data == {
            'first': {'a': 1},
            'second': {'b': 2},
        }


class TestSelfReferentialProjection:
    def test_self_referential_serializer_accepts_nested_projection(self):
        class NodeSerializer(serializers.Serializer):
            name = serializers.CharField()

        NodeSerializer._declared_fields['children'] = NodeSerializer(many=True)

        instance = {
            'name': 'root',
            'children': [{'name': 'child', 'children': []}],
        }

        assert NodeSerializer(
            instance,
            fields=['name', 'children.name'],
        ).data == {
            'name': 'root',
            'children': [{'name': 'child'}],
        }


class TestProjectionDoesNotAffectDeserialization:
    def test_required_and_validators_remain_active(self):
        class InputSerializer(serializers.Serializer):
            title = serializers.CharField()
            token = serializers.CharField(
                write_only=True,
                required=True,
                min_length=3,
            )

        missing_token = InputSerializer(
            data={'title': 'example'},
            fields=['title'],
        )
        assert not missing_token.is_valid()
        assert 'token' in missing_token.errors

        invalid_token = InputSerializer(
            data={'title': 'example', 'token': 'ab'},
            fields=['title'],
        )
        assert not invalid_token.is_valid()
        assert 'token' in invalid_token.errors

        valid = InputSerializer(
            data={'title': 'example', 'token': 'abc'},
            fields=['title'],
        )
        assert valid.is_valid(), valid.errors
        assert valid.validated_data == {'title': 'example', 'token': 'abc'}
        assert valid.data == {'title': 'example'}
        assert valid.fields['token'].required is True
        assert any(
            isinstance(validator, MinLengthValidator)
            for validator in valid.fields['token'].validators
        )

    def test_write_only_fields_cannot_be_projected(self):
        class InputSerializer(serializers.Serializer):
            title = serializers.CharField()
            token = serializers.CharField(write_only=True)

        with pytest.raises(ValueError, match='write-only'):
            InputSerializer(fields=['token'])

    def test_projection_does_not_change_class_fields(self, book):
        projected = BookSerializer(book, fields=['name'])
        assert projected.data == {'name': 'Book'}

        later = BookSerializer(book)
        assert later.data['name'] == 'Book'
        assert later.data['author'] == {
            'email': 'a@example.com',
            'secret': 'secret',
        }
        assert 'summary' in later.data
        assert 'omitted' in later.data
