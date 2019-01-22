import logging
import re

from django.db import transaction
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from edx_rest_api_client.client import EdxRestApiClient
from rest_framework import status, viewsets
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from course_discovery.apps.api import filters, serializers
from course_discovery.apps.api.pagination import ProxiedPagination
from course_discovery.apps.api.utils import get_query_param
from course_discovery.apps.course_metadata.choices import CourseRunStatus
from course_discovery.apps.course_metadata.constants import COURSE_ID_REGEX, COURSE_UUID_REGEX
from course_discovery.apps.course_metadata.models import Course, CourseEntitlement, CourseRun, Organization, SeatType

logger = logging.getLogger(__name__)


# pylint: disable=no-member
class CourseViewSet(viewsets.ModelViewSet):
    """ Course resource. """
    filter_backends = (DjangoFilterBackend,)
    filter_class = filters.CourseFilter
    lookup_field = 'key'
    lookup_value_regex = COURSE_ID_REGEX + '|' + COURSE_UUID_REGEX
    serializer_class = serializers.CourseWithProgramsSerializer

    course_key_regex = re.compile(COURSE_ID_REGEX)
    course_uuid_regex = re.compile(COURSE_UUID_REGEX)

    # Explicitly support PageNumberPagination and LimitOffsetPagination. Future
    # versions of this API should only support the system default, PageNumberPagination.
    pagination_class = ProxiedPagination

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())

        key = self.kwargs['key']

        if self.course_key_regex.match(key):
            filter_key = 'key'
        elif self.course_uuid_regex.match(key):
            filter_key = 'uuid'

        filter_kwargs = {filter_key: key}
        obj = get_object_or_404(queryset, **filter_kwargs)

        # May raise a permission denied
        self.check_object_permissions(self.request, obj)

        return obj

    def get_queryset(self):
        partner = self.request.site.partner
        q = self.request.query_params.get('q')

        if q:
            queryset = Course.search(q)
            queryset = self.get_serializer_class().prefetch_queryset(queryset=queryset, partner=partner)
        else:
            if get_query_param(self.request, 'include_hidden_course_runs'):
                course_runs = CourseRun.objects.filter(course__partner=partner)
            else:
                course_runs = CourseRun.objects.filter(course__partner=partner).exclude(hidden=True)

            if get_query_param(self.request, 'marketable_course_runs_only'):
                course_runs = course_runs.marketable().active()

            if get_query_param(self.request, 'marketable_enrollable_course_runs_with_archived'):
                course_runs = course_runs.marketable().enrollable()

            if get_query_param(self.request, 'published_course_runs_only'):
                course_runs = course_runs.filter(status=CourseRunStatus.Published)

            queryset = self.get_serializer_class().prefetch_queryset(
                queryset=self.queryset,
                course_runs=course_runs,
                partner=partner
            )

        return queryset.order_by(Lower('key'))

    def get_serializer_context(self, *args, **kwargs):
        context = super().get_serializer_context(*args, **kwargs)
        query_params = ['exclude_utm', 'include_deleted_programs']

        for query_param in query_params:
            context[query_param] = get_query_param(self.request, query_param)

        return context

    def get_course_key(self, data):
        return '{org}+{number}'.format(org=data['org'], number=data['number'])

    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        """
        if self.action == 'list' or self.action == 'retrieve':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser]
        return [permission() for permission in permission_classes]

    def create(self, request, *args, **kwargs):
        """
        Create a Course
        """
        course_data = request.data
        error_messages = []
        if not (course_data.get('title') and course_data.get('number') and course_data.get('org') and
                course_data.get('mode')):
            error_messages.append('Not all required fields provided.')
        if not Organization.objects.filter(key=course_data.get('org')).exists():
            error_messages.append('Organization does not exist.')
        if not SeatType.objects.filter(slug=course_data.get('mode')).exists():
            error_messages.append('Entitlement Track does not exist.')
        if error_messages:
            return Response(
                {
                    'messages': ['Incorrect data sent.'] + error_messages,
                    'data': {
                        'title': course_data.get('title'),
                        'number': course_data.get('number'),
                        'org': course_data.get('org'),
                        'mode': course_data.get('mode'),
                        'price': course_data.get('price'),
                    },
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        else:
            partner = request.site.partner
            course_data['partner'] = partner.id
            course_data['key'] = self.get_course_key(course_data)
            serializer = self.get_serializer(data=course_data)
            serializer.is_valid(raise_exception=True)
            try:
                with transaction.atomic():
                    self.perform_create(serializer)
                    course = Course.objects.get(uuid=serializer.validated_data['uuid'])

                    price = course_data.get('price', 0.00)
                    ecom_api = EdxRestApiClient(partner.ecommerce_api_url, jwt=partner.access_token)
                    ecom_entitlement_data = {
                        'product_class': 'Course Entitlement',
                        'name': course.title,
                        'price': price,
                        'certificate_type': course_data.get('mode'),
                        'uuid': str(course.uuid),
                    }
                    ecom_api.products.post(ecom_entitlement_data)

                    mode = SeatType.objects.get(slug=course_data.get('mode'))
                    CourseEntitlement.objects.create(
                        course=course,
                        mode=mode,
                        partner=partner,
                        price=price,
                    )
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    'An error occurred while creating the course [%s].', serializer.validated_data['title']
                )
                return Response('Failed to add course data.', status=status.HTTP_400_BAD_REQUEST)
            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def list(self, request, *args, **kwargs):
        """ List all courses.
         ---
        parameters:
            - name: exclude_utm
              description: Exclude UTM parameters from marketing URLs.
              required: false
              type: integer
              paramType: query
              multiple: false
            - name: include_deleted_programs
              description: Will include deleted programs in the associated programs array
              required: false
              type: integer
              paramType: query
              multiple: false
            - name: keys
              description: Filter by keys (comma-separated list)
              required: false
              type: string
              paramType: query
              multiple: false
            - name: include_hidden_course_runs
              description: Include course runs that are hidden in the response.
              required: false
              type: integer
              paramType: query
              mulitple: false
            - name: marketable_course_runs_only
              description: Restrict returned course runs to those that are published, have seats,
                and are enrollable or will be enrollable in the future
              required: false
              type: integer
              paramType: query
              mulitple: false
            - name: marketable_enrollable_course_runs_with_archived
              description: Restrict returned course runs to those that are published, have seats,
                and can be enrolled in now. Includes archived courses.
              required: false
              type: integer
              paramType: query
              mulitple: false
            - name: published_course_runs_only
              description: Filter course runs by published ones only
              required: false
              type: integer
              paramType: query
              mulitple: false
            - name: q
              description: Elasticsearch querystring query. This filter takes precedence over other filters.
              required: false
              type: string
              paramType: query
              multiple: false
        """
        return super(CourseViewSet, self).list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        """ Retrieve details for a course. """
        return super(CourseViewSet, self).retrieve(request, *args, **kwargs)
