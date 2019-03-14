from django.conf import settings
from django.conf.urls import url
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse_lazy, reverse
from django.db import transaction, connection
from django.db.models import TextField, Q
from django.forms import ModelForm, ModelMultipleChoiceField
from django.http import HttpResponseRedirect, Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext_lazy as _, ugettext, ungettext
from reversion.admin import VersionAdmin

from judge.models import Contest, ContestProblem, ContestSubmission, Profile, Rating
from judge.ratings import rate_contest
from judge.widgets import HeavySelect2Widget, HeavySelect2MultipleWidget, AdminPagedownWidget, Select2MultipleWidget, \
    HeavyPreviewAdminPageDownWidget, Select2Widget


class HeavySelect2Widget(HeavySelect2Widget):
    @property
    def is_hidden(self):
        return False


class ContestTagForm(ModelForm):
    contests = ModelMultipleChoiceField(
        label=_('Included contests'),
        queryset=Contest.objects.all(),
        required=False,
        widget=HeavySelect2MultipleWidget(data_view='contest_select2'))


class ContestTagAdmin(admin.ModelAdmin):
    fields = ('name', 'color', 'description', 'contests')
    list_display = ('name', 'color')
    actions_on_top = True
    actions_on_bottom = True
    form = ContestTagForm

    if AdminPagedownWidget is not None:
        formfield_overrides = {
            TextField: {'widget': AdminPagedownWidget},
        }

    def save_model(self, request, obj, form, change):
        super(ContestTagAdmin, self).save_model(request, obj, form, change)
        obj.contests = form.cleaned_data['contests']

    def get_form(self, request, obj=None, **kwargs):
        form = super(ContestTagAdmin, self).get_form(request, obj, **kwargs)
        if obj is not None:
            form.base_fields['contests'].initial = obj.contests.all()
        return form


class ContestProblemInlineForm(ModelForm):
    class Meta:
        widgets = {'problem': HeavySelect2Widget(data_view='problem_select2')}


class ContestProblemInline(admin.TabularInline):
    model = ContestProblem
    verbose_name = _('Problem')
    verbose_name_plural = 'Problems'
    fields = ('problem', 'points', 'partial', 'is_pretested', 'max_submissions', 'output_prefix_override', 'order',
              'rejudge_column')
    readonly_fields = ('rejudge_column',)
    form = ContestProblemInlineForm

    def rejudge_column(self, obj):
        if obj.id is None:
            return ''
        return '<a class="button rejudge-link" href="%s">Rejudge</a>' % reverse('admin:judge_contest_rejudge',
                                                                                args=(obj.contest.id, obj.id))
    rejudge_column.short_description = ''
    rejudge_column.allow_tags = True


class ContestForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(ContestForm, self).__init__(*args, **kwargs)
        if 'rate_exclude' in self.fields:
            self.fields['rate_exclude'].queryset = \
                Profile.objects.filter(contest_history__contest=self.instance).distinct()
        self.fields['banned_users'].widget.can_add_related = False

    def clean(self):
        cleaned_data = super(ContestForm, self).clean()
        cleaned_data['banned_users'].filter(current_contest__contest=self.instance).update(current_contest=None)

    class Meta:
        widgets = {
            'organizers': HeavySelect2MultipleWidget(data_view='profile_select2'),
            'organizations': HeavySelect2MultipleWidget(data_view='organization_select2'),
            'tags': Select2MultipleWidget,
            'banned_users': HeavySelect2MultipleWidget(data_view='profile_select2', attrs={'style': 'width: 100%'}),
        }

        if HeavyPreviewAdminPageDownWidget is not None:
            widgets['description'] = HeavyPreviewAdminPageDownWidget(preview=reverse_lazy('contest_preview'))
            widgets['registration_page'] = HeavyPreviewAdminPageDownWidget(preview=reverse_lazy('contest_preview'))


class ContestAdmin(VersionAdmin):
    fieldsets = (
        (None,              {'fields': ('key', 'name', 'organizers')}),
        (_('Settings'),     {'fields': ('is_public', 'is_virtualable', 'use_clarifications', 'hide_problem_tags',
                                        'freeze_submissions', 'hide_scoreboard', 'run_pretests_only', 'access_code')}),
        (_('Bonuses'),      {'fields': ('time_bonus', 'first_submission_bonus')}),
        (_('Scheduling'),   {'fields': ('start_time', 'end_time', 'time_limit')}),
        (_('Details'),      {'fields': ('description', 'og_image', 'logo_override_image', 'tags', 'summary')}),
        (_('Rating'),       {'fields': ('is_rated', 'rate_all', 'rate_exclude')}),
        (_('Registration'), {'fields': ('require_registration', 'registration_start_time', 'registration_end_time', 'registration_page')}),
        (_('Organization'), {'fields': ('is_private', 'is_private_viewable', 'organizations')}),
        (_('Justice'),      {'fields': ('banned_users',)}),
    )
    list_display = ('key', 'name', 'is_public', 'is_rated', 'start_time', 'end_time',
                    'time_limit', 'time_bonus', 'first_submission_bonus', 'user_count')
    actions = ['make_public', 'make_private']
    inlines = [ContestProblemInline]
    actions_on_top = True
    actions_on_bottom = True
    form = ContestForm
    change_list_template = 'admin/judge/contest/change_list.html'
    filter_horizontal = ['rate_exclude']
    date_hierarchy = 'start_time'

    def get_queryset(self, request):
        queryset = Contest.objects.all()
        if request.user.has_perm('judge.edit_all_contest'):
            return queryset
        else:
            return queryset.filter(organizers__id=request.user.profile.id)

    def get_readonly_fields(self, request, obj=None):
        readonly = []
        if not request.user.has_perm('judge.contest_frozen_state') or (obj is not None and obj.freeze_submissions):
            readonly += ['freeze_submissions']
        if not request.user.has_perm('judge.contest_rating'):
            readonly += ['is_rated', 'rate_all', 'rate_exclude']
        if not request.user.has_perm('judge.contest_access_code'):
            readonly += ['access_code']
        return readonly

    def has_change_permission(self, request, obj=None):
        if not request.user.has_perm('judge.edit_own_contest'):
            return False
        if request.user.has_perm('judge.edit_all_contest') or obj is None:
            return True
        return obj.organizers.filter(id=request.user.profile.id).exists()

    def make_public(self, request, queryset):
        count = queryset.update(is_public=True)
        self.message_user(request, ungettext('%d contest successfully marked as public.',
                                             '%d contests successfully marked as public.',
                                             count) % count)
    make_public.short_description = _('Mark contests as public')

    def make_private(self, request, queryset):
        count = queryset.update(is_public=False)
        self.message_user(request, ungettext('%d contest successfully marked as private.',
                                             '%d contests successfully marked as private.',
                                             count) % count)
    make_private.short_description = _('Mark contests as private')

    def get_urls(self):
        return [url(r'^rate/all/$', self.rate_all_view, name='judge_contest_rate_all'),
                url(r'^(\d+)/rate/$', self.rate_view, name='judge_contest_rate'),
                url(r'^(\d+)/judge/(\d+)/$', self.rejudge_view, name='judge_contest_rejudge'),
                url(r'^(\d+)/unfreeze/$', self.unfreeze_view, name='judge_contest_unfreeze')] + super(ContestAdmin, self).get_urls()

    def rejudge_view(self, request, contest_id, problem_id):
        if not request.user.has_perm('judge.rejudge_submission'):
            self.message_user(request, ugettext('You do not have the permission to rejudge submissions.'),
                              level=messages.ERROR)
            return

        queryset = ContestSubmission.objects.filter(problem_id=problem_id).select_related('submission')
        if not request.user.has_perm('judge.rejudge_submission_lot') and \
                len(queryset) > getattr(settings, 'REJUDGE_SUBMISSION_LIMIT', 10):
            self.message_user(request, ugettext('You do not have the permission to rejudge THAT many submissions.'),
                              level=messages.ERROR)
            return

        for model in queryset:
            model.submission.judge(rejudge=True)

        self.message_user(request, ungettext('%d submission were successfully scheduled for rejudging.',
                                             '%d submissions were successfully scheduled for rejudging.',
                                             len(queryset)) % len(queryset))
        return HttpResponseRedirect(reverse('admin:judge_contest_change', args=(contest_id,)))

    def unfreeze_view(self, request, id):
        if not request.user.has_perm('judge.contest_frozen_state'):
            raise PermissionDenied()
        contest = get_object_or_404(Contest, id=id)
        if not contest.freeze_submissions:
            raise Http404()
        with transaction.atomic():
            contest.freeze_submissions = False
            contest.save()
            for submission in ContestSubmission.objects.filter(updated_frozen=True, participation__contest=contest):
                submission.submission.recalculate_contest_submission()
        return HttpResponseRedirect(reverse('admin:judge_contest_change', args=(id,)))

    def rate_all_view(self, request):
        if not request.user.has_perm('judge.contest_rating'):
            raise PermissionDenied()
        with transaction.atomic():
            if connection.vendor == 'sqlite':
                Rating.objects.all().delete()
            else:
                cursor = connection.cursor()
                cursor.execute('TRUNCATE TABLE `%s`' % Rating._meta.db_table)
                cursor.close()
            Profile.objects.update(rating=None)
            for contest in Contest.objects.filter(is_rated=True).order_by('end_time'):
                rate_contest(contest)
        return HttpResponseRedirect(reverse('admin:judge_contest_changelist'))

    def rate_view(self, request, id):
        if not request.user.has_perm('judge.contest_rating'):
            raise PermissionDenied()
        contest = get_object_or_404(Contest, id=id)
        if not contest.is_rated:
            raise Http404()
        with transaction.atomic():
            Rating.objects.filter(contest__end_time__gte=contest.end_time).delete()
            for contest in Contest.objects.filter(is_rated=True, end_time__gte=contest.end_time).order_by('end_time'):
                rate_contest(contest)
        return HttpResponseRedirect(request.META.get('HTTP_REFERER', reverse('admin:judge_contest_changelist')))

    def get_form(self, *args, **kwargs):
        form = super(ContestAdmin, self).get_form(*args, **kwargs)
        perms = ('edit_own_contest', 'edit_all_contest')
        form.base_fields['organizers'].queryset = Profile.objects.filter(
            Q(user__is_superuser=True) |
            Q(user__groups__permissions__codename__in=perms) |
            Q(user__user_permissions__codename__in=perms)
        ).distinct()
        return form


class ContestParticipationForm(ModelForm):
    class Meta:
        widgets = {
            'contest': Select2Widget(),
            'user': HeavySelect2Widget(data_view='profile_select2'),
        }


class ContestParticipationAdmin(admin.ModelAdmin):
    fields = ('contest', 'user', 'real_start', 'virtual')
    list_display = ('contest', 'username', 'show_virtual', 'real_start', 'score', 'cumtime')
    actions = ['recalculate_points', 'recalculate_cumtime']
    actions_on_bottom = actions_on_top = True
    search_fields = ('contest__key', 'contest__name', 'user__user__username')
    form = ContestParticipationForm
    date_hierarchy = 'real_start'

    def get_queryset(self, request):
        return super(ContestParticipationAdmin, self).get_queryset(request).only(
            'contest__name', 'user__user__username', 'real_start', 'score', 'cumtime', 'virtual'
        )

    def recalculate_points(self, request, queryset):
        count = 0
        for participation in queryset:
            participation.recalculate_score()
            count += 1
        self.message_user(request, ungettext('%d participation have scores recalculated.',
                                             '%d participations have scores recalculated.',
                                             count) % count)
    recalculate_points.short_description = _('Recalculate scores')

    def recalculate_cumtime(self, request, queryset):
        count = 0
        for participation in queryset:
            participation.update_cumtime()
            count += 1
        self.message_user(request, ungettext('%d participation have times recalculated.',
                                             '%d participations have times recalculated.',
                                             count) % count)
    recalculate_cumtime.short_description = _('Recalculate cumulative time')

    def username(self, obj):
        return obj.user.username
    username.short_description = _('username')
    username.admin_order_field = 'user__user__username'

    def show_virtual(self, obj):
        return obj.virtual or '-'
    show_virtual.short_description = _('virtual')
    show_virtual.admin_order_field = 'virtual'

class ContestRegistrationAdmin(admin.ModelAdmin):
    fields = ('contest', 'user', 'registration_time', 'data')
    list_display = ('contest', 'username', 'registration_time')
    search_fields = ('contest__key', 'contest__name', 'user__user__username', 'user__name')
    form = ContestParticipationForm
    date_hierarchy = 'registration_time'

    def username(self, obj):
        return obj.user.long_display_name
    username.short_description = _('username')
    username.admin_order_field = 'user__user__username'

