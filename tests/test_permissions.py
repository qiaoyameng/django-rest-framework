import base64
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Group, Permission, User
from django.db import models
from django.test import TestCase, override_settings
from django.urls import ResolverMatch

from rest_framework import (
    HTTP_HEADER_ENCODING, authentication, generics, permissions, serializers,
    status, views
)
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter
from rest_framework.test import APIRequestFactory
from tests.models import BasicModel

factory = APIRequestFactory()


class BasicSerializer(serializers.ModelSerializer):
    class Meta:
        model = BasicModel
        fields = '__all__'


class RootView(generics.ListCreateAPIView):
    queryset = BasicModel.objects.all()
    serializer_class = BasicSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [permissions.DjangoModelPermissions]


class InstanceView(generics.RetrieveUpdateDestroyAPIView):
    queryset = BasicModel.objects.all()
    serializer_class = BasicSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [permissions.DjangoModelPermissions]


class GetQuerySetListView(generics.ListCreateAPIView):
    serializer_class = BasicSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [permissions.DjangoModelPermissions]

    def get_queryset(self):
        return BasicModel.objects.all()


class EmptyListView(generics.ListCreateAPIView):
    queryset = BasicModel.objects.none()
    serializer_class = BasicSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [permissions.DjangoModelPermissions]


class IgnoredGetQuerySetListView(GetQuerySetListView):
    _ignore_model_permissions = True


root_view = RootView.as_view()
api_root_view = DefaultRouter().get_api_root_view()
instance_view = InstanceView.as_view()
get_queryset_list_view = GetQuerySetListView.as_view()
empty_list_view = EmptyListView.as_view()
ignored_get_queryset_list_view = IgnoredGetQuerySetListView.as_view()


def basic_auth_header(username, password):
    credentials = ('%s:%s' % (username, password))
    base64_credentials = base64.b64encode(credentials.encode(HTTP_HEADER_ENCODING)).decode(HTTP_HEADER_ENCODING)
    return 'Basic %s' % base64_credentials


@pytest.mark.usefixtures("reset_sequences")
class ModelPermissionsIntegrationTests(TestCase):
    def setUp(self):
        User.objects.create_user('disallowed', 'disallowed@example.com', 'password')
        user = User.objects.create_user('permitted', 'permitted@example.com', 'password')
        user.user_permissions.set([
            Permission.objects.get(codename='add_basicmodel'),
            Permission.objects.get(codename='change_basicmodel'),
            Permission.objects.get(codename='delete_basicmodel')
        ])

        user = User.objects.create_user('updateonly', 'updateonly@example.com', 'password')
        user.user_permissions.set([
            Permission.objects.get(codename='change_basicmodel'),
        ])

        self.permitted_credentials = basic_auth_header('permitted', 'password')
        self.disallowed_credentials = basic_auth_header('disallowed', 'password')
        self.updateonly_credentials = basic_auth_header('updateonly', 'password')

        BasicModel(text='foo').save()

    def test_has_create_permissions(self):
        request = factory.post('/', {'text': 'foobar'}, format='json',
                               HTTP_AUTHORIZATION=self.permitted_credentials)
        response = root_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_api_root_view_discard_default_django_model_permission(self):
        """
        We check that DEFAULT_PERMISSION_CLASSES can
        apply to APIRoot view. More specifically we check expected behavior of
        ``_ignore_model_permissions`` attribute support.
        """
        request = factory.get('/', format='json',
                              HTTP_AUTHORIZATION=self.permitted_credentials)
        request.resolver_match = ResolverMatch('get', (), {})
        response = api_root_view(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_ignore_model_permissions_with_unauthenticated_user(self):
        """
        We check that the ``_ignore_model_permissions`` attribute
        doesn't ignore the authentication.
        """
        request = factory.get('/', format='json')
        request.resolver_match = ResolverMatch('get', (), {})
        response = ignored_get_queryset_list_view(request)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_ignore_model_permissions_with_authenticated_user(self):
        """
        We check that the ``_ignore_model_permissions`` attribute
        with an authenticated user.
        """
        request = factory.get('/', format='json',
                              HTTP_AUTHORIZATION=self.permitted_credentials)
        request.resolver_match = ResolverMatch('get', (), {})
        response = ignored_get_queryset_list_view(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_get_queryset_has_create_permissions(self):
        request = factory.post('/', {'text': 'foobar'}, format='json',
                               HTTP_AUTHORIZATION=self.permitted_credentials)
        response = get_queryset_list_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_has_put_permissions(self):
        request = factory.put('/1', {'text': 'foobar'}, format='json',
                              HTTP_AUTHORIZATION=self.permitted_credentials)
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_has_delete_permissions(self):
        request = factory.delete('/1', HTTP_AUTHORIZATION=self.permitted_credentials)
        response = instance_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_does_not_have_create_permissions(self):
        request = factory.post('/', {'text': 'foobar'}, format='json',
                               HTTP_AUTHORIZATION=self.disallowed_credentials)
        response = root_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_does_not_have_put_permissions(self):
        request = factory.put('/1', {'text': 'foobar'}, format='json',
                              HTTP_AUTHORIZATION=self.disallowed_credentials)
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_does_not_have_delete_permissions(self):
        request = factory.delete('/1', HTTP_AUTHORIZATION=self.disallowed_credentials)
        response = instance_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_options_permitted(self):
        request = factory.options(
            '/',
            HTTP_AUTHORIZATION=self.permitted_credentials
        )
        response = root_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('actions', response.data)
        self.assertEqual(list(response.data['actions']), ['POST'])

        request = factory.options(
            '/1',
            HTTP_AUTHORIZATION=self.permitted_credentials
        )
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('actions', response.data)
        self.assertEqual(list(response.data['actions']), ['PUT'])

    def test_options_disallowed(self):
        request = factory.options(
            '/',
            HTTP_AUTHORIZATION=self.disallowed_credentials
        )
        response = root_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('actions', response.data)

        request = factory.options(
            '/1',
            HTTP_AUTHORIZATION=self.disallowed_credentials
        )
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('actions', response.data)

    def test_options_updateonly(self):
        request = factory.options(
            '/',
            HTTP_AUTHORIZATION=self.updateonly_credentials
        )
        response = root_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('actions', response.data)

        request = factory.options(
            '/1',
            HTTP_AUTHORIZATION=self.updateonly_credentials
        )
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('actions', response.data)
        self.assertEqual(list(response.data['actions']), ['PUT'])

    def test_empty_view_does_not_assert(self):
        request = factory.get('/1', HTTP_AUTHORIZATION=self.permitted_credentials)
        response = empty_list_view(request, pk=1)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_calling_method_not_allowed(self):
        request = factory.generic('METHOD_NOT_ALLOWED', '/', HTTP_AUTHORIZATION=self.permitted_credentials)
        response = root_view(request)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        request = factory.generic('METHOD_NOT_ALLOWED', '/1', HTTP_AUTHORIZATION=self.permitted_credentials)
        response = instance_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_check_auth_before_queryset_call(self):
        class View(RootView):
            def get_queryset(_):
                self.fail('should not reach due to auth check')
        view = View.as_view()

        request = factory.get('/', HTTP_AUTHORIZATION='')
        response = view(request)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_queryset_assertions(self):
        class View(views.APIView):
            authentication_classes = [authentication.BasicAuthentication]
            permission_classes = [permissions.DjangoModelPermissions]
        view = View.as_view()

        request = factory.get('/', HTTP_AUTHORIZATION=self.permitted_credentials)
        msg = 'Cannot apply DjangoModelPermissions on a view that does not set `.queryset` or have a `.get_queryset()` method.'
        with self.assertRaisesMessage(AssertionError, msg):
            view(request)

        # Faulty `get_queryset()` methods should trigger the above "view does not have a queryset" assertion.
        class View(RootView):
            def get_queryset(self):
                return None
        view = View.as_view()

        request = factory.get('/', HTTP_AUTHORIZATION=self.permitted_credentials)
        with self.assertRaisesMessage(AssertionError, 'View.get_queryset() returned None'):
            view(request)


class BasicPermModel(models.Model):
    text = models.CharField(max_length=100)

    class Meta:
        app_label = 'tests'


class BasicPermSerializer(serializers.ModelSerializer):
    class Meta:
        model = BasicPermModel
        fields = '__all__'


# Custom object-level permission, that includes 'view' permissions
class ViewObjectPermissions(permissions.DjangoObjectPermissions):
    perms_map = {
        'GET': ['%(app_label)s.view_%(model_name)s'],
        'OPTIONS': ['%(app_label)s.view_%(model_name)s'],
        'HEAD': ['%(app_label)s.view_%(model_name)s'],
        'POST': ['%(app_label)s.add_%(model_name)s'],
        'PUT': ['%(app_label)s.change_%(model_name)s'],
        'PATCH': ['%(app_label)s.change_%(model_name)s'],
        'DELETE': ['%(app_label)s.delete_%(model_name)s'],
    }


class ObjectPermissionInstanceView(generics.RetrieveUpdateDestroyAPIView):
    queryset = BasicPermModel.objects.all()
    serializer_class = BasicPermSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [ViewObjectPermissions]


object_permissions_view = ObjectPermissionInstanceView.as_view()


class ObjectPermissionListView(generics.ListAPIView):
    queryset = BasicPermModel.objects.all()
    serializer_class = BasicPermSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [ViewObjectPermissions]


object_permissions_list_view = ObjectPermissionListView.as_view()


class GetQuerysetObjectPermissionInstanceView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = BasicPermSerializer
    authentication_classes = [authentication.BasicAuthentication]
    permission_classes = [ViewObjectPermissions]

    def get_queryset(self):
        return BasicPermModel.objects.all()


get_queryset_object_permissions_view = GetQuerysetObjectPermissionInstanceView.as_view()


@pytest.mark.usefixtures("reset_sequences")
@unittest.skipUnless('guardian' in settings.INSTALLED_APPS, 'django-guardian not installed')
class ObjectPermissionsIntegrationTests(TestCase):
    """
    Integration tests for the object level permissions API.
    """
    def setUp(self):
        from guardian.shortcuts import assign_perm

        # create users
        create = User.objects.create_user
        users = {
            'fullaccess': create('fullaccess', 'fullaccess@example.com', 'password'),
            'readonly': create('readonly', 'readonly@example.com', 'password'),
            'writeonly': create('writeonly', 'writeonly@example.com', 'password'),
            'deleteonly': create('deleteonly', 'deleteonly@example.com', 'password'),
        }

        # give everyone model level permissions, as we are not testing those
        everyone = Group.objects.create(name='everyone')
        model_name = BasicPermModel._meta.model_name
        app_label = BasicPermModel._meta.app_label
        f = '{}_{}'.format
        perms = {
            'view': f('view', model_name),
            'change': f('change', model_name),
            'delete': f('delete', model_name)
        }
        for perm in perms.values():
            perm = f'{app_label}.{perm}'
            assign_perm(perm, everyone)
        everyone.user_set.add(*users.values())

        # appropriate object level permissions
        readers = Group.objects.create(name='readers')
        writers = Group.objects.create(name='writers')
        deleters = Group.objects.create(name='deleters')

        model = BasicPermModel.objects.create(text='foo')

        assign_perm(perms['view'], readers, model)
        assign_perm(perms['change'], writers, model)
        assign_perm(perms['delete'], deleters, model)

        readers.user_set.add(users['fullaccess'], users['readonly'])
        writers.user_set.add(users['fullaccess'], users['writeonly'])
        deleters.user_set.add(users['fullaccess'], users['deleteonly'])

        self.credentials = {}
        for user in users.values():
            self.credentials[user.username] = basic_auth_header(user.username, 'password')

    # Delete
    def test_can_delete_permissions(self):
        request = factory.delete('/1', HTTP_AUTHORIZATION=self.credentials['deleteonly'])
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_cannot_delete_permissions(self):
        request = factory.delete('/1', HTTP_AUTHORIZATION=self.credentials['readonly'])
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # Update
    def test_can_update_permissions(self):
        request = factory.patch(
            '/1', {'text': 'foobar'}, format='json',
            HTTP_AUTHORIZATION=self.credentials['writeonly']
        )
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get('text'), 'foobar')

    def test_cannot_update_permissions(self):
        request = factory.patch(
            '/1', {'text': 'foobar'}, format='json',
            HTTP_AUTHORIZATION=self.credentials['deleteonly']
        )
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_update_permissions_non_existing(self):
        request = factory.patch(
            '/999', {'text': 'foobar'}, format='json',
            HTTP_AUTHORIZATION=self.credentials['deleteonly']
        )
        response = object_permissions_view(request, pk='999')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Read
    def test_can_read_permissions(self):
        request = factory.get('/1', HTTP_AUTHORIZATION=self.credentials['readonly'])
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cannot_read_permissions(self):
        request = factory.get('/1', HTTP_AUTHORIZATION=self.credentials['writeonly'])
        response = object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_can_read_get_queryset_permissions(self):
        """
        same as ``test_can_read_permissions`` but with a view
        that rely on ``.get_queryset()`` instead of ``.queryset``.
        """
        request = factory.get('/1', HTTP_AUTHORIZATION=self.credentials['readonly'])
        response = get_queryset_object_permissions_view(request, pk='1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # Read list
    # Note: this previously tested `DjangoObjectPermissionsFilter`, which has
    # since been moved to a separate package. These now act as sanity checks.
    def test_can_read_list_permissions(self):
        request = factory.get('/', HTTP_AUTHORIZATION=self.credentials['readonly'])
        response = object_permissions_list_view(request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0].get('id'), 1)

    def test_cannot_method_not_allowed(self):
        request = factory.generic('METHOD_NOT_ALLOWED', '/', HTTP_AUTHORIZATION=self.credentials['readonly'])
        response = object_permissions_list_view(request)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class BasicPerm(permissions.BasePermission):
    def has_permission(self, request, view):
        return False


class BasicPermWithDetail(permissions.BasePermission):
    message = 'Custom: You cannot access this resource'
    code = 'permission_denied_custom'

    def has_permission(self, request, view):
        return False


class BasicObjectPerm(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        return False


class BasicObjectPermWithDetail(permissions.BasePermission):
    message = 'Custom: You cannot access this resource'
    code = 'permission_denied_custom'

    def has_object_permission(self, request, view, obj):
        return False


class PermissionInstanceView(generics.RetrieveUpdateDestroyAPIView):
    queryset = BasicModel.objects.all()
    serializer_class = BasicSerializer


class DeniedView(PermissionInstanceView):
    permission_classes = (BasicPerm,)


class DeniedViewWithDetail(PermissionInstanceView):
    permission_classes = (BasicPermWithDetail,)


class DeniedObjectView(PermissionInstanceView):
    permission_classes = (BasicObjectPerm,)


class DeniedObjectViewWithDetail(PermissionInstanceView):
    permission_classes = (BasicObjectPermWithDetail,)


denied_view = DeniedView.as_view()

denied_view_with_detail = DeniedViewWithDetail.as_view()

denied_object_view = DeniedObjectView.as_view()

denied_object_view_with_detail = DeniedObjectViewWithDetail.as_view()


@pytest.mark.usefixtures("reset_sequences")
class CustomPermissionsTests(TestCase):
    def setUp(self):
        BasicModel(text='foo').save()
        User.objects.create_user('username', 'username@example.com', 'password')
        credentials = basic_auth_header('username', 'password')
        self.request = factory.get('/1', format='json', HTTP_AUTHORIZATION=credentials)
        self.custom_message = 'Custom: You cannot access this resource'
        self.custom_code = 'permission_denied_custom'

    def test_permission_denied(self):
        response = denied_view(self.request, pk=1)
        detail = response.data.get('detail')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotEqual(detail, self.custom_message)
        self.assertNotEqual(detail.code, self.custom_code)

    def test_permission_denied_with_custom_detail(self):
        response = denied_view_with_detail(self.request, pk=1)
        detail = response.data.get('detail')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(detail, self.custom_message)
        self.assertEqual(detail.code, self.custom_code)

    def test_permission_denied_for_object(self):
        response = denied_object_view(self.request, pk=1)
        detail = response.data.get('detail')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotEqual(detail, self.custom_message)
        self.assertNotEqual(detail.code, self.custom_code)

    def test_permission_denied_for_object_with_custom_detail(self):
        response = denied_object_view_with_detail(self.request, pk=1)
        detail = response.data.get('detail')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(detail, self.custom_message)
        self.assertEqual(detail.code, self.custom_code)


class PermissionsCompositionTests(TestCase):

    def setUp(self):
        self.username = 'john'
        self.email = 'lennon@thebeatles.com'
        self.password = 'password'
        self.user = User.objects.create_user(
            self.username,
            self.email,
            self.password
        )
        self.client.login(username=self.username, password=self.password)

    def test_and_false(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()
        composed_perm = permissions.IsAuthenticated & permissions.AllowAny
        assert composed_perm().has_permission(request, None) is False

    def test_and_true(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = permissions.IsAuthenticated & permissions.AllowAny
        assert composed_perm().has_permission(request, None) is True

    def test_or_false(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()
        composed_perm = permissions.IsAuthenticated | permissions.AllowAny
        assert composed_perm().has_permission(request, None) is True

    def test_or_true(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = permissions.IsAuthenticated | permissions.AllowAny
        assert composed_perm().has_permission(request, None) is True

    def test_not_false(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()
        composed_perm = ~permissions.IsAuthenticated
        assert composed_perm().has_permission(request, None) is True

    def test_not_true(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = ~permissions.AllowAny
        assert composed_perm().has_permission(request, None) is False

    def test_several_levels_without_negation(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = (
            permissions.IsAuthenticated &
            permissions.IsAuthenticated &
            permissions.IsAuthenticated &
            permissions.IsAuthenticated
        )
        assert composed_perm().has_permission(request, None) is True

    def test_several_levels_and_precedence_with_negation(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = (
            permissions.IsAuthenticated &
            ~ permissions.IsAdminUser &
            permissions.IsAuthenticated &
            ~(permissions.IsAdminUser & permissions.IsAdminUser)
        )
        assert composed_perm().has_permission(request, None) is True

    def test_several_levels_and_precedence(self):
        request = factory.get('/1', format='json')
        request.user = self.user
        composed_perm = (
            permissions.IsAuthenticated &
            permissions.IsAuthenticated |
            permissions.IsAuthenticated &
            permissions.IsAuthenticated
        )
        assert composed_perm().has_permission(request, None) is True

    def test_or_laziness(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()

        with mock.patch.object(permissions.AllowAny, 'has_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.AllowAny | permissions.IsAuthenticated)
                hasperm = composed_perm().has_permission(request, None)
                assert hasperm is True
                assert mock_allow.call_count == 1
                mock_deny.assert_not_called()

        with mock.patch.object(permissions.AllowAny, 'has_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.IsAuthenticated | permissions.AllowAny)
                hasperm = composed_perm().has_permission(request, None)
                assert hasperm is True
                assert mock_deny.call_count == 1
                assert mock_allow.call_count == 1

    def test_object_or_laziness(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()

        with mock.patch.object(permissions.AllowAny, 'has_object_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_object_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.AllowAny | permissions.IsAuthenticated)
                hasperm = composed_perm().has_object_permission(request, None, None)
                assert hasperm is True
                assert mock_allow.call_count == 1
                mock_deny.assert_not_called()

        with mock.patch.object(permissions.AllowAny, 'has_object_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_object_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.IsAuthenticated | permissions.AllowAny)
                hasperm = composed_perm().has_object_permission(request, None, None)
                assert hasperm is True
                assert mock_deny.call_count == 0
                assert mock_allow.call_count == 1

    def test_and_laziness(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()

        with mock.patch.object(permissions.AllowAny, 'has_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.AllowAny & permissions.IsAuthenticated)
                hasperm = composed_perm().has_permission(request, None)
                assert hasperm is False
                assert mock_allow.call_count == 1
                assert mock_deny.call_count == 1

        with mock.patch.object(permissions.AllowAny, 'has_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.IsAuthenticated & permissions.AllowAny)
                hasperm = composed_perm().has_permission(request, None)
                assert hasperm is False
                assert mock_deny.call_count == 1
                mock_allow.assert_not_called()

    def test_object_and_laziness(self):
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()

        with mock.patch.object(permissions.AllowAny, 'has_object_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_object_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.AllowAny & permissions.IsAuthenticated)
                hasperm = composed_perm().has_object_permission(request, None, None)
                assert hasperm is False
                assert mock_allow.call_count == 1
                assert mock_deny.call_count == 1

        with mock.patch.object(permissions.AllowAny, 'has_object_permission', return_value=True) as mock_allow:
            with mock.patch.object(permissions.IsAuthenticated, 'has_object_permission', return_value=False) as mock_deny:
                composed_perm = (permissions.IsAuthenticated & permissions.AllowAny)
                hasperm = composed_perm().has_object_permission(request, None, None)
                assert hasperm is False
                assert mock_deny.call_count == 1
                mock_allow.assert_not_called()

    def test_unimplemented_has_object_permission(self):
        "test for issue 6402 https://github.com/encode/django-rest-framework/issues/6402"
        request = factory.get('/1', format='json')
        request.user = AnonymousUser()

        class IsAuthenticatedUserOwner(permissions.IsAuthenticated):
            def has_object_permission(self, request, view, obj):
                return True

        composed_perm = (IsAuthenticatedUserOwner | permissions.IsAdminUser)
        hasperm = composed_perm().has_object_permission(request, None, None)
        assert hasperm is False

    def test_operand_holder_is_hashable(self):
        assert hash(permissions.IsAuthenticated & permissions.IsAdminUser)

    def test_operand_holder_hash_same_for_same_operands_and_operator(self):
        first_operand_holder = (
            permissions.IsAuthenticated & permissions.IsAdminUser
        )
        second_operand_holder = (
            permissions.IsAuthenticated & permissions.IsAdminUser
        )

        assert hash(first_operand_holder) == hash(second_operand_holder)

    def test_operand_holder_hash_differs_for_different_operands(self):
        first_operand_holder = (
            permissions.IsAuthenticated & permissions.IsAdminUser
        )
        second_operand_holder = (
            permissions.AllowAny & permissions.IsAdminUser
        )
        third_operand_holder = (
            permissions.IsAuthenticated & permissions.AllowAny
        )

        assert hash(first_operand_holder) != hash(second_operand_holder)
        assert hash(first_operand_holder) != hash(third_operand_holder)
        assert hash(second_operand_holder) != hash(third_operand_holder)

    def test_operand_holder_hash_differs_for_different_operators(self):
        first_operand_holder = (
            permissions.IsAuthenticated & permissions.IsAdminUser
        )
        second_operand_holder = (
            permissions.IsAuthenticated | permissions.IsAdminUser
        )

        assert hash(first_operand_holder) != hash(second_operand_holder)

    def test_filtering_permissions(self):
        unfiltered_permissions = [
            permissions.IsAuthenticated & permissions.IsAdminUser,
            permissions.IsAuthenticated & permissions.IsAdminUser,
            permissions.AllowAny,
        ]
        expected_permissions = [
            permissions.IsAuthenticated & permissions.IsAdminUser,
            permissions.AllowAny,
        ]

        filtered_permissions = [
            perm for perm
            in dict.fromkeys(unfiltered_permissions)
        ]

        assert filtered_permissions == expected_permissions


class EventPermission(permissions.BasePermission):
    label = None
    message = 'permission message'
    code = 'permission_code'

    def has_permission(self, request, view):
        request.events.append(f'{self.label}:view')
        return request.permission_results[(self.label, 'view')]

    def has_object_permission(self, request, view, obj):
        request.events.append(f'{self.label}:object')
        return request.permission_results[(self.label, 'object')]


class EventA(EventPermission):
    label = 'A'
    message = 'A denied'
    code = 'a_denied'


class EventB(EventPermission):
    label = 'B'
    message = 'B denied'
    code = 'b_denied'


class EventC(EventPermission):
    label = 'C'
    message = 'C denied'
    code = 'c_denied'


class EventD(EventPermission):
    label = 'D'
    message = 'D denied'
    code = 'd_denied'


class ComplexPermissionView(views.APIView):
    permission_classes = [(EventA | EventB) & (EventC | ~EventD)]
    permission_recording = True

    def get(self, request):
        self.check_object_permissions(request, 'first')
        self.check_object_permissions(request, 'second')
        return Response({'ok': True})


class ComplexPermissionViewWithoutRecording(ComplexPermissionView):
    permission_recording = False


class DenyLeaf(permissions.BasePermission):
    message = 'Leaf denied'
    code = 'leaf_denied'

    def has_permission(self, request, view):
        return False


class DeniedCompositeView(views.APIView):
    permission_classes = [EventA & DenyLeaf]
    permission_recording = True
    authentication_classes = []


class ViewBoomPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        recording_test_requests.append(request)
        raise RuntimeError('view boom')


class ObjectBoomPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return True

    def has_object_permission(self, request, view, obj):
        recording_test_requests.append(request)
        raise ValueError('object boom')


class SilentPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return True

    def has_object_permission(self, request, view, obj):
        return True


recording_test_requests = []


class PermissionRecordingTests(TestCase):
    expected_events = [
        'A:view', 'C:view', 'D:view',
        'A:view', 'A:object', 'C:view', 'D:view', 'D:object',
        'A:view', 'A:object', 'C:view', 'D:view', 'D:object',
    ]

    def setUp(self):
        recording_test_requests.clear()

    def complex_request(self):
        request = factory.get('/')
        request.events = []
        request.permission_results = {
            ('A', 'view'): True,
            ('A', 'object'): True,
            ('B', 'view'): True,
            ('B', 'object'): True,
            ('C', 'view'): False,
            ('C', 'object'): True,
            ('D', 'view'): False,
            ('D', 'object'): False,
        }
        return request

    def leaf(self, node, label):
        if node['kind'] == 'PERMISSION':
            return node if node['permission'].endswith(label) else None
        for child in node['children']:
            found = self.leaf(child, label)
            if found is not None:
                return found

    def leaf_at_stage(self, node, label, stage):
        if node['kind'] == 'PERMISSION':
            if node['permission'].endswith(label) and node['stage'] == stage:
                return node
        else:
            for child in node['children']:
                found = self.leaf_at_stage(child, label, stage)
                if found is not None:
                    return found

    def evaluated_leaves(self, node):
        if node['kind'] == 'PERMISSION':
            return [node] if node['status'] == 'evaluated' else []
        leaves = []
        for child in node['children']:
            leaves.extend(self.evaluated_leaves(child))
        return leaves

    def test_recording_preserves_results_and_call_order(self):
        disabled_request = self.complex_request()
        disabled_response = ComplexPermissionViewWithoutRecording.as_view()(disabled_request)

        enabled_request = self.complex_request()
        enabled_response = ComplexPermissionView.as_view()(enabled_request)

        assert disabled_response.status_code == status.HTTP_200_OK
        assert enabled_response.status_code == status.HTTP_200_OK
        assert disabled_request.events == self.expected_events
        assert enabled_request.events == self.expected_events
        assert disabled_response.renderer_context['request'].permission_recorder is None

        recorder = enabled_response.renderer_context['request'].permission_recorder
        records = recorder.to_dict()
        assert [record['stage'] for record in records] == ['view', 'object', 'object']
        assert records[1]['object'] == "'first'"
        assert records[2]['object'] == "'second'"

        view_root = records[0]['checks'][0]
        assert view_root['kind'] == 'AND'
        assert view_root['result'] is True
        assert self.leaf(view_root, 'A')['result'] is True
        assert self.leaf(view_root, 'B')['status'] == 'unevaluated'
        assert self.leaf(view_root, 'B')['result'] is None
        assert self.leaf(view_root, 'C')['result'] is False
        assert self.leaf(view_root, 'D')['result'] is False
        assert self.leaf(view_root, 'D')['message'] == 'D denied'
        assert self.leaf(view_root, 'D')['code'] == 'd_denied'

        orders = [
            leaf['order']
            for record in records
            for leaf in self.evaluated_leaves(record['checks'][0])
        ]
        assert orders == list(range(len(self.expected_events)))

        for record in records[1:]:
            object_root = record['checks'][0]
            first_or, second_or = object_root['children']
            assert first_or['kind'] == 'OR'
            assert first_or['children'][0]['synthetic'] is True
            assert self.leaf_at_stage(first_or, 'A', 'view')['result'] is True
            assert self.leaf_at_stage(first_or, 'A', 'object')['result'] is True
            assert self.leaf(first_or, 'B')['status'] == 'unevaluated'

            assert second_or['children'][0]['synthetic'] is True
            assert self.leaf_at_stage(second_or, 'C', 'view')['result'] is False
            assert self.leaf_at_stage(second_or, 'C', 'object')['status'] == 'unevaluated'
            assert self.leaf_at_stage(second_or, 'D', 'view')['result'] is False
            assert self.leaf_at_stage(second_or, 'D', 'object')['result'] is False

    def test_default_403_response_is_unchanged_and_records_leaf_detail(self):
        request = self.complex_request()
        response = DeniedCompositeView.as_view()(request)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert str(response.data['detail']) == (
            'You do not have permission to perform this action.'
        )
        assert response.data['detail'].code == 'permission_denied'

        recorder = response.renderer_context['request'].permission_recorder
        root = recorder.to_dict()[0]['checks'][0]
        deny_leaf = self.leaf(root, 'DenyLeaf')
        assert deny_leaf['result'] is False
        assert deny_leaf['message'] == 'Leaf denied'
        assert deny_leaf['code'] == 'leaf_denied'

    def test_exception_handler_can_read_recorder_without_changing_response(self):
        def recording_exception_handler(exc, context):
            self.handler_records = context['request'].permission_recorder.to_dict()
            return views.exception_handler(exc, context)

        original_handler = views.api_settings.EXCEPTION_HANDLER
        views.api_settings.EXCEPTION_HANDLER = recording_exception_handler
        try:
            response = DeniedCompositeView.as_view()(self.complex_request())
        finally:
            views.api_settings.EXCEPTION_HANDLER = original_handler

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert str(response.data['detail']) == (
            'You do not have permission to perform this action.'
        )
        root = self.handler_records[0]['checks'][0]
        assert self.leaf(root, 'DenyLeaf')['code'] == 'leaf_denied'

    @override_settings(REST_FRAMEWORK={'PERMISSION_RECORDING': True})
    def test_global_setting_enables_recorder(self):
        class GlobalRecordingView(views.APIView):
            authentication_classes = []
            permission_classes = [DenyLeaf]

            permission_recording = views.api_settings.PERMISSION_RECORDING

        response = GlobalRecordingView.as_view()(factory.get('/'))
        recorder = response.renderer_context['request'].permission_recorder

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert recorder.to_dict()[0]['checks'][0]['permission'].endswith('DenyLeaf')

    def test_recorder_can_evaluate_directly_without_extra_invocations(self):
        recorder = permissions.PermissionRecorder()
        request = self.complex_request()
        permission = (EventA | EventB) & (EventC | ~EventD)

        result = recorder.evaluate_permission(permission(), request, None)

        assert result is True
        assert request.events == ['A:view', 'C:view', 'D:view']
        root = recorder.to_dict()[0]['checks'][0]
        assert self.leaf(root, 'B')['status'] == 'unevaluated'

    def test_view_exception_is_preserved_and_short_circuit_is_recorded(self):
        class BoomView(views.APIView):
            permission_classes = [ViewBoomPermission & SilentPermission]
            permission_recording = True

        request = factory.get('/')
        with pytest.raises(RuntimeError, match='view boom'):
            BoomView.as_view()(request)

        root = recording_test_requests[-1].permission_recorder.to_dict()[0]['checks'][0]
        boom_leaf = root['children'][0]
        silent_leaf = root['children'][1]
        assert root['status'] == 'error'
        assert boom_leaf['status'] == 'error'
        assert boom_leaf['exception']['type'] == 'RuntimeError'
        assert boom_leaf['exception']['message'] == 'view boom'
        assert silent_leaf['status'] == 'unevaluated'

    def test_object_exception_is_preserved(self):
        class BoomObjectView(views.APIView):
            permission_classes = [ObjectBoomPermission]
            permission_recording = True

            def get(self, request):
                self.check_object_permissions(request, None)

        request = factory.get('/')
        with pytest.raises(ValueError, match='object boom'):
            BoomObjectView.as_view()(request)

        root = recording_test_requests[-1].permission_recorder.to_dict()[1]['checks'][0]
        assert root['status'] == 'error'
        assert root['exception']['type'] == 'ValueError'
        assert root['exception']['message'] == 'object boom'

    def test_records_are_isolated_between_concurrent_requests(self):
        barrier = Barrier(8)

        def make_request(index):
            request = self.complex_request()
            request.permission_results[('A', 'view')] = True
            barrier.wait()
            response = ComplexPermissionView.as_view()(request)
            recorder = response.renderer_context['request'].permission_recorder
            return (
                response.status_code,
                request.events,
                recorder.to_dict(),
                recorder,
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(make_request, range(8)))

        assert len({id(result[3]) for result in results}) == 8
        for status_code, events, records, recorder in results:
            assert status_code == status.HTTP_200_OK
            assert events == self.expected_events
            assert len(records) == 3
            assert {record['stage'] for record in records} == {'view', 'object'}
