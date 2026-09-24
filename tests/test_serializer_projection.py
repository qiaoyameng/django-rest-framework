"""
Tests for explicit output field projection via the `only` argument.

`only` restricts serialization output to a set of dotted field paths,
using public field names (never `source` attribute names). Projection
affects output only: deserialization, `required` and validators are
left untouched.
"""
import pytest
from django.core.exceptions import ImproperlyConfigured

from rest_framework import serializers


class TrackedObject:
    """
    An object that records how many times each attribute is accessed,
    so tests can prove that excluded fields are never touched.
    """

    def __init__(self, **attrs):
        self.access_counts = {key: 0 for key in attrs}
        self._attrs = attrs

    def __getattr__(self, name):
        # Only called for names not found normally, i.e. the tracked
        # attributes stored in `_attrs`.
        try:
            attrs = self.__dict__['_attrs']
            value = attrs[name]
        except KeyError:
            raise AttributeError(name)
        self.access_counts[name] += 1
        return value


class TrackedSerializer(serializers.Serializer):
    name = serializers.CharField()
    email = serializers.CharField()
    computed = serializers.SerializerMethodField()

    method_calls = 0

    def get_computed(self, obj):
        type(self).method_calls += 1
        return 'computed:' + obj.name


@pytest.fixture(autouse=True)
def reset_method_calls():
    TrackedSerializer.method_calls = 0
    yield
    TrackedSerializer.method_calls = 0


class TestProjectionExcludesWork:
    """
    Excluded fields must not perform attribute access or run
    SerializerMethodField methods.
    """

    def test_excluded_field_does_not_access_attribute(self):
        obj = TrackedObject(name='alice', email='alice@example.com')
        serializer = TrackedSerializer(obj, only=['name'])
        assert serializer.data == {'name': 'alice'}
        assert obj.access_counts == {'name': 1, 'email': 0}

    def test_excluded_method_field_is_not_called(self):
        obj = TrackedObject(name='alice', email='alice@example.com')
        serializer = TrackedSerializer(obj, only=['name'])
        assert serializer.data == {'name': 'alice'}
        assert TrackedSerializer.method_calls == 0

    def test_selected_fields_still_execute(self):
        obj = TrackedObject(name='alice', email='alice@example.com')
        serializer = TrackedSerializer(obj, only=['name', 'computed'])
        assert serializer.data == {'name': 'alice', 'computed': 'computed:alice'}
        assert TrackedSerializer.method_calls == 1

    def test_no_projection_returns_full_representation(self):
        obj = TrackedObject(name='alice', email='alice@example.com')
        serializer = TrackedSerializer(obj)
        assert serializer.data == {
            'name': 'alice',
            'email': 'alice@example.com',
            'computed': 'computed:alice',
        }
        assert obj.access_counts == {'name': 2, 'email': 1}
        assert TrackedSerializer.method_calls == 1


class AuthorSerializer(serializers.Serializer):
    email = serializers.CharField()
    bio = serializers.CharField()


class ItemSerializer(serializers.Serializer):
    name = serializers.CharField()
    price = serializers.IntegerField()


class OrderSerializer(serializers.Serializer):
    code = serializers.CharField()
    author = AuthorSerializer()
    items = ItemSerializer(many=True)


def make_order(**overrides):
    author = TrackedObject(email='a@example.com', bio='bio')
    items = overrides.pop('items', [
        TrackedObject(name='pen', price=10),
        TrackedObject(name='book', price=20),
    ])
    order = TrackedObject(code='X1', author=author, items=items)
    return order


class TestNestedProjection:
    def test_dotted_path_selects_nested_field(self):
        order = make_order()
        serializer = OrderSerializer(order, only=['code', 'author.email'])
        assert serializer.data == {
            'code': 'X1',
            'author': {'email': 'a@example.com'},
        }
        # The excluded nested field was never accessed.
        assert order.author.access_counts == {'email': 1, 'bio': 0}
        assert order.access_counts['items'] == 0

    def test_nested_many_projection(self):
        order = make_order()
        serializer = OrderSerializer(order, only=['items.name'])
        assert serializer.data == {
            'items': [{'name': 'pen'}, {'name': 'book'}],
        }
        for item in order.items:
            assert item.access_counts == {'name': 1, 'price': 0}

    def test_nested_many_with_none_value(self):
        order = make_order(items=None)
        serializer = OrderSerializer(order, only=['code', 'items.name'])
        assert serializer.data == {'code': 'X1', 'items': None}

    def test_nested_many_with_empty_list(self):
        order = make_order(items=[])
        serializer = OrderSerializer(order, only=['items.name'])
        assert serializer.data == {'items': []}

    def test_parent_and_child_paths_merge_to_parent(self):
        # A bare parent path selects the whole subtree, regardless of
        # whether it appears before or after the more specific path.
        for only in (['author', 'author.email'], ['author.email', 'author']):
            order = make_order()
            serializer = OrderSerializer(order, only=only)
            assert serializer.data == {
                'author': {'email': 'a@example.com', 'bio': 'bio'},
            }

    def test_top_level_many_projection(self):
        items = [TrackedObject(name='pen', price=10)]
        serializer = ItemSerializer(items, many=True, only=['name'])
        assert serializer.data == [{'name': 'pen'}]
        assert items[0].access_counts == {'name': 1, 'price': 0}

    def test_list_serializer_direct_projection(self):
        items = [TrackedObject(name='pen', price=10)]
        list_serializer = serializers.ListSerializer(
            child=ItemSerializer(), only=['name']
        )
        list_serializer.instance = items
        assert list_serializer.data == [{'name': 'pen'}]


class TestProjectionValidation:
    def test_unknown_field_raises_before_serialization(self):
        obj = TrackedObject(name='alice', email='alice@example.com')
        serializer = TrackedSerializer(obj, only=['name', 'unknown'])
        with pytest.raises(ImproperlyConfigured, match='unknown'):
            serializer.data
        # The error was raised before any attribute access happened.
        assert obj.access_counts == {'name': 0, 'email': 0}
        assert TrackedSerializer.method_calls == 0

    def test_unknown_nested_field_raises(self):
        order = make_order()
        serializer = OrderSerializer(order, only=['author.unknown'])
        with pytest.raises(ImproperlyConfigured, match='author.unknown'):
            serializer.data

    def test_sub_path_of_non_serializer_field_raises(self):
        obj = TrackedObject(name='alice', email='alice@example.com')
        serializer = TrackedSerializer(obj, only=['name.first'])
        with pytest.raises(ImproperlyConfigured, match='name'):
            serializer.data

    def test_invalid_empty_path_raises(self):
        obj = TrackedObject(name='alice', email='alice@example.com')
        serializer = TrackedSerializer(obj, only=['name..email'])
        with pytest.raises(ImproperlyConfigured):
            serializer.data


class TestProjectionUsesPublicFieldNames:
    def test_source_attribute_name_is_not_exposed(self):
        class UserSerializer(serializers.Serializer):
            email = serializers.CharField(source='contact')

        obj = TrackedObject(contact='a@example.com')
        serializer = UserSerializer(obj, only=['contact'])
        with pytest.raises(ImproperlyConfigured, match='contact'):
            serializer.data

    def test_public_name_maps_to_source(self):
        class UserSerializer(serializers.Serializer):
            email = serializers.CharField(source='contact')

        obj = TrackedObject(contact='a@example.com')
        serializer = UserSerializer(obj, only=['email'])
        assert serializer.data == {'email': 'a@example.com'}
        assert obj.access_counts == {'contact': 1}

    def test_starred_source_nested_serializer(self):
        class FlatSerializer(serializers.Serializer):
            a = serializers.IntegerField()
            b = serializers.IntegerField()

        class OuterSerializer(serializers.Serializer):
            id = serializers.IntegerField()
            flat = FlatSerializer(source='*')

        obj = TrackedObject(id=1, a=2, b=3)
        serializer = OuterSerializer(obj, only=['flat.a'])
        assert serializer.data == {'flat': {'a': 2}}
        assert obj.access_counts == {'id': 0, 'a': 1, 'b': 0}


class TestProjectionInstanceIsolation:
    def test_two_projections_of_same_serializer_class(self):
        first = TrackedSerializer(
            TrackedObject(name='a', email='a@example.com'), only=['name']
        )
        second = TrackedSerializer(
            TrackedObject(name='b', email='b@example.com'), only=['email']
        )
        assert first.data == {'name': 'a'}
        assert second.data == {'email': 'b@example.com'}

    def test_projection_does_not_mutate_class_fields(self):
        order = make_order()
        OrderSerializer(order, only=['author.email']).data
        assert set(OrderSerializer._declared_fields) == {'code', 'author', 'items'}
        assert set(AuthorSerializer._declared_fields) == {'email', 'bio'}

    def test_next_instantiation_is_unaffected(self):
        order = make_order()
        OrderSerializer(order, only=['code']).data
        fresh = OrderSerializer(make_order())
        assert fresh.data == {
            'code': 'X1',
            'author': {'email': 'a@example.com', 'bio': 'bio'},
            'items': [
                {'name': 'pen', 'price': 10},
                {'name': 'book', 'price': 20},
            ],
        }


class TestProjectionOutputOnly:
    """
    Projection must not change deserialization, `required` or validators.
    """

    def test_required_still_enforced_for_excluded_fields(self):
        class SignupSerializer(serializers.Serializer):
            name = serializers.CharField()
            age = serializers.IntegerField()

        serializer = SignupSerializer(data={'name': 'alice'}, only=['name'])
        assert not serializer.is_valid()
        assert set(serializer.errors) == {'age'}

    def test_validated_data_is_complete(self):
        class SignupSerializer(serializers.Serializer):
            name = serializers.CharField()
            age = serializers.IntegerField()

        serializer = SignupSerializer(
            data={'name': 'alice', 'age': 30}, only=['name']
        )
        assert serializer.is_valid()
        assert serializer.validated_data == {'name': 'alice', 'age': 30}

    def test_validators_still_run_for_excluded_fields(self):
        class SignupSerializer(serializers.Serializer):
            name = serializers.CharField()
            age = serializers.IntegerField(min_value=18)

        serializer = SignupSerializer(
            data={'name': 'alice', 'age': 3}, only=['name']
        )
        assert not serializer.is_valid()
        assert set(serializer.errors) == {'age'}

    def test_write_only_field_selected_in_projection(self):
        class CredentialsSerializer(serializers.Serializer):
            username = serializers.CharField()
            password = serializers.CharField(write_only=True)

        obj = TrackedObject(username='u', password='p')
        serializer = CredentialsSerializer(obj, only=['username', 'password'])
        # write_only fields never appear in output, even when selected.
        assert serializer.data == {'username': 'u'}

        serializer = CredentialsSerializer(
            data={'username': 'u', 'password': 'p'}, only=['username']
        )
        assert serializer.is_valid()
        assert serializer.validated_data == {'username': 'u', 'password': 'p'}


class TestSelfReferencingProjection:
    def test_recursive_serializer_projection(self):
        class NodeSerializer(serializers.Serializer):
            name = serializers.CharField()
            value = serializers.IntegerField()

            def get_fields(self):
                fields = super().get_fields()
                fields['children'] = NodeSerializer(many=True, required=False)
                return fields

        child = TrackedObject(name='child', value=2, children=[])
        root = TrackedObject(name='root', value=1, children=[child])
        serializer = NodeSerializer(root, only=['name', 'children.name'])
        assert serializer.data == {
            'name': 'root',
            'children': [{'name': 'child'}],
        }
        assert root.access_counts['value'] == 0
        assert child.access_counts['value'] == 0
