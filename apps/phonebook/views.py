from functools import wraps

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.http import Http404, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import redirect, render, get_object_or_404
from django.views.decorators.cache import cache_page, never_cache
from django.views.decorators.http import require_POST

import commonware.log
from funfactory.urlresolvers import reverse
from tower import ugettext as _

from apps.groups.helpers import stringify_groups
from apps.groups.models import Group
from apps.users.models import COUNTRIES, UserProfile
from apps.users.tasks import remove_from_basket_task

import forms
from models import Invite


log = commonware.log.getLogger('m.phonebook')

BAD_VOUCHER = 'Unknown Voucher'


def vouch_required(f):
    """If a user is not vouched they get a 403."""

    @login_required
    @wraps(f)
    def wrapped(request, *args, **kwargs):
        if request.user.get_profile().is_vouched:
            return f(request, *args, **kwargs)
        else:
            log.warning('vouch_required forbidding access')
            return HttpResponseForbidden(_('You must be vouched to do this.'))

    return wrapped


@never_cache
def home(request):
    if request.user.is_authenticated():
        profile = request.user.get_profile()
        my_groups = profile.groups.exclude(steward=None).order_by('name')
        curated_groups = Group.get_curated()
        data = dict(groups=my_groups, curated_groups=curated_groups)
        return render(request, 'phonebook/home.html', data)
    else:
        return render(request, 'phonebook/home.html')


@never_cache
@login_required
def profile(request, username):
    """View a profile by username."""
    user = get_object_or_404(User, username=username)
    vouch_form = None
    profile = user.get_profile()

    if not profile.is_complete:
        raise Http404()

    if not request.user.userprofile.is_vouched and request.user != user:
        log.warning('vouch_required forbidding access')
        return HttpResponseForbidden(_('You must be vouched to do this.'))

    if not profile.is_vouched and request.user.get_profile().is_vouched:
        vouch_form = forms.VouchForm(initial=dict(vouchee=profile.pk))

    data = dict(shown_user=user, profile=profile, vouch_form=vouch_form)
    return render(request, 'phonebook/profile.html', data)


@never_cache
@login_required
def edit_profile(request):
    """Edit user profile view."""
    # Don't user request.user
    user = User.objects.get(pk=request.user.id)
    profile = user.get_profile()
    user_groups = stringify_groups(profile.groups.all().order_by('name'))
    user_skills = stringify_groups(profile.skills.all().order_by('name'))
    user_languages = stringify_groups(profile.languages.all().order_by('name'))

    user_form = forms.UserForm(request.POST or None, instance=user)
    profile_form = forms.ProfileForm(
        request.POST or None, request.FILES or None, instance=profile,
        initial=dict(groups=user_groups, skills=user_skills,
                     languages=user_languages),
        locale=request.locale)

    if request.method == 'POST':
        if (user_form.is_valid() and profile_form.is_valid()):
            old_username = request.user.username
            user_form.save()
            profile_form.save()

            # Notify the user that their old profile URL won't work.
            if user.username != old_username:
                messages.info(request, _(u'You changed your username; please '
                                         'note your profile URL has also '
                                         'changed.'))
            return redirect(reverse('profile', args=[user.username]))

    d = dict(profile_form=profile_form,
             user_form=user_form,
             mode='edit',
             user_groups=user_groups,
             my_vouches=UserProfile.objects.filter(vouched_by=profile),
             profile=profile,
             apps=user.apiapp_set.filter(is_active=True))

    # If there are form errors, don't send a 200 OK.
    status = 400 if (profile_form.errors or user_form.errors) else 200
    return render(request, 'phonebook/edit_profile.html', d, status=status)


@never_cache
@login_required
def confirm_delete(request):
    """Display a confirmation page asking the user if they want to
    leave.

    """
    return render(request, 'phonebook/confirm_delete.html')


@never_cache
@login_required
@require_POST
def delete(request):
    user_profile = request.user.get_profile()
    remove_from_basket_task.delay(user_profile.id)
    user_profile.anonymize()
    log.info('Deleting %d' % user_profile.user.id)
    logout(request)
    return redirect(reverse('home'))


@vouch_required
def search(request):
    num_pages = 0
    limit = None
    people = []
    show_pagination = False
    form = forms.SearchForm(request.GET)
    groups = None
    curated_groups = None

    if form.is_valid():
        query = form.cleaned_data.get('q', u'')
        limit = form.cleaned_data['limit']
        include_non_vouched = form.cleaned_data['include_non_vouched']
        page = request.GET.get('page', 1)
        curated_groups = Group.get_curated()


        profiles = UserProfile.search(
            query, include_non_vouched=include_non_vouched)
        groups = Group.search(query)

        paginator = Paginator(profiles, limit)

        try:
            people = paginator.page(page)
        except PageNotAnInteger:
            people = paginator.page(1)
        except EmptyPage:
            people = paginator.page(paginator.num_pages)

        if profiles.count() == 1 and not groups:
            return redirect(reverse('profile', args=[people[0].user.username]))

        if paginator.count > forms.PAGINATION_LIMIT:
            show_pagination = True
            num_pages = len(people.paginator.page_range)

    d = dict(people=people,
             form=form,
             limit=limit,
             show_pagination=show_pagination,
             num_pages=num_pages,
             groups=groups,
             curated_groups=curated_groups)

    if request.is_ajax():
        return render(request, 'search_ajax.html', d)

    return render(request, 'phonebook/search.html', d)


@cache_page(60 * 60 * 168)  # 1 week.
def search_plugin(request):
    """Render an OpenSearch Plugin."""
    return render(request, 'phonebook/search_opensearch.xml',
                  content_type='application/opensearchdescription+xml')


@vouch_required
def invite(request):
    profile = request.user.userprofile
    invite_form = forms.InviteForm(request.POST or None,
                                   instance=Invite(inviter=profile))
    if request.method == 'POST' and invite_form.is_valid():
        invite = invite_form.save()
        invite.send(sender=profile)
        return render(request, 'phonebook/invited.html', {'recipient': invite.recipient })

    return render(request, 'phonebook/invite.html', {'invite_form':invite_form})


@vouch_required
@require_POST
def vouch(request):
    """Vouch a user."""
    form = forms.VouchForm(request.POST)

    if form.is_valid():
        p = UserProfile.objects.get(pk=form.cleaned_data.get('vouchee'))
        p.vouch(request.user.get_profile())

        # Notify the current user that they vouched successfully.
        msg = _(u'Thanks for vouching for a fellow Mozillian! '
                 'This user is now vouched!')
        messages.info(request, msg)
        return redirect(reverse('profile', args=[p.user.username]))

    return HttpResponseForbidden


@vouch_required
def list_country(request, country, region=None, city=None):
    country = country.lower()
    country_name = COUNTRIES.get(country, None)
    queryset = (UserProfile.objects.exclude(full_name='')
                .filter(is_vouched=True).filter(country=country))
    if city:
        queryset = queryset.filter(city__iexact=city)
    if region:
        queryset = queryset.filter(region__iexact=region)

    data = {'people': queryset,
            'country_name': country_name,
            'city_name': city,
            'region_name': region}
    return render(request, 'phonebook/location-list.html', data)
