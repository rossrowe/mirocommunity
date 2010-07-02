# Copyright 2009 - Participatory Culture Foundation
#
# This file is part of Miro Community.
#
# Miro Community is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# Miro Community is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Miro Community.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import time
import urllib
import urlparse

from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.core import cache
from django.db.models import Q
try:
    from django.core.validators import URLValidator
    url_re = URLValidator.regex # Django 1.2+
except ImportError:
    from django.forms.fields import url_re
from django.http import HttpResponseRedirect, Http404
from django.shortcuts import render_to_response
from django.template import RequestContext, Context, loader
from django.views.decorators.csrf import csrf_protect

from localtv import models, util
from localtv.decorators import get_sitelocation, request_passes_test
from localtv.submit_video import forms
from localtv.submit_video.util import is_video_url

def _check_submit_permissions(request):
    sitelocation = models.SiteLocation.objects.get(
        site=Site.objects.get_current())
    user = request.user
    if not sitelocation.submission_requires_login:
        return True
    else:
        if sitelocation.display_submit_button:
            return user.is_authenticated() and user.is_active
        else:
            return sitelocation.user_is_admin(user)


def submit_lock(func):
    def wrapper(request, *args, **kwargs):
        if request.method != 'POST':
            return func(request, *args, **kwargs)
        cache_key = 'submit_lock.%s.%s.%s' % (
            kwargs['sitelocation'].site.domain,
            request.path,
            request.POST['url'])
        while True:
            stored = cache.cache.add(cache_key, 'locked', 20)
            if stored:
                # got the lock
                break
            time.sleep(0.25)
        response = func(request, *args, **kwargs)
        # release the lock
        cache.cache.delete(cache_key)
        return response
    return wrapper


@request_passes_test(_check_submit_permissions)
@get_sitelocation
@csrf_protect
def submit_video(request, sitelocation=None):
    if not (sitelocation.display_submit_button or
            sitelocation.user_is_admin(request.user)):
        raise Http404
    url = request.POST.get('url') or request.GET.get('url', '')
    if request.method == "GET" and not url:
        submit_form = forms.SubmitVideoForm()
        return render_to_response(
            'localtv/submit_video/submit.html',
            {'sitelocation': sitelocation,
             'form': submit_form},
            context_instance=RequestContext(request))
    else:
        url = urlparse.urldefrag(url)[0]
        submit_form = forms.SubmitVideoForm({'url': url or ''})
        if submit_form.is_valid():
            existing = models.Video.objects.filter(
                Q(website_url=submit_form.cleaned_data['url']) |
                Q(file_url=submit_form.cleaned_data['url']),
                site=sitelocation.site)
            existing.filter(status=models.VIDEO_STATUS_REJECTED).delete()
            if existing.count():
                if sitelocation.user_is_admin(request.user):
                    # even if the video was rejected, an admin submitting it
                    # should make it approved
                    for v in existing.exclude(
                        status=models.VIDEO_STATUS_ACTIVE):
                        v.user = request.user
                        v.status = models.VIDEO_STATUS_ACTIVE
                        v.when_approved = datetime.datetime.now()
                        v.save()
                    return HttpResponseRedirect(
                        reverse('localtv_submit_thanks',
                                args=[existing[0].pk]))
                else:
                    # pick the first approved video to point the user at
                    videos = existing.filter(
                        status=models.VIDEO_STATUS_ACTIVE)
                    if videos.count():
                        video = videos[0]
                    else:
                        video = None
                    return render_to_response(
                        'localtv/submit_video/submit.html',
                        {'sitelocation': sitelocation,
                         'form': forms.SubmitVideoForm(),
                         'was_duplicate': True,
                         'video': video},
                        context_instance=RequestContext(request))

            scraped_data = util.get_scraped_data(
                submit_form.cleaned_data['url'])

            get_dict = {'url': submit_form.cleaned_data['url']}
            if 'bookmarklet' in request.GET:
                get_dict['bookmarklet'] = '1'
            get_params = urllib.urlencode(get_dict)
            if scraped_data:
                if 'link' in scraped_data and \
                        scraped_data['link'] != get_dict['url']:
                    request.POST = {
                        'url': scraped_data['link'].encode('utf8')}
                    # rerun the view, but with the canonical URL
                    return submit_video(request)

                if (scraped_data.get('embed')
                    or (scraped_data.get('file_url')
                        and not scraped_data.get('file_url_is_flaky'))):
                    return HttpResponseRedirect(
                        reverse('localtv_submit_scraped_video') + '?' +
                        get_params)

            # otherwise if it looks like a video file
            if is_video_url(submit_form.cleaned_data['url']):
                return HttpResponseRedirect(
                    reverse('localtv_submit_directlink_video')
                    + '?' + get_params)
            else:
                return HttpResponseRedirect(
                    reverse('localtv_submit_embedrequest_video')
                    + '?' + get_params)

        else:
            return render_to_response(
                'localtv/submit_video/submit.html',
                {'sitelocation': sitelocation,
                 'form': submit_form},
                context_instance=RequestContext(request))


def _submit_finish(form, *args, **kwargs):
    if form.is_valid():
        video = form.save()
        models.submit_finished.send(sender=video)

        #redirect to a thank you page
        return HttpResponseRedirect(reverse('localtv_submit_thanks',
                                            args=[video.pk]))

    else:
        return render_to_response(*args, **kwargs)


@request_passes_test(_check_submit_permissions)
@get_sitelocation
@submit_lock
@csrf_protect
def scraped_submit_video(request, sitelocation=None):
    if not (request.REQUEST.get('url') and \
                url_re.match(request.REQUEST['url'])):
        return HttpResponseRedirect(reverse('localtv_submit_video'))

    scraped_data = util.get_scraped_data(request.REQUEST['url'])

    url = scraped_data.get('link', request.REQUEST['url'])
    existing =  models.Video.objects.filter(site=sitelocation.site,
                                            website_url=url)
    existing.filter(status=models.VIDEO_STATUS_REJECTED).delete()
    if existing.count():
        return HttpResponseRedirect(reverse('localtv_submit_thanks',
                                                args=[existing[0].id]))
    initial = dict(request.GET.items())
    if request.method == "GET":
        scraped_form = forms.ScrapedSubmitVideoForm(initial=initial,
                                                    scraped_data=scraped_data)

        return render_to_response(
            'localtv/submit_video/scraped.html',
            {'sitelocation': sitelocation,
             'data': scraped_data,
             'form': scraped_form},
            context_instance=RequestContext(request))

    scraped_form = forms.ScrapedSubmitVideoForm(request.POST,
                                                sitelocation=sitelocation,
                                                user=request.user,
                                                scraped_data=scraped_data)
    return _submit_finish(scraped_form,
            'localtv/submit_video/scraped.html',
            {'sitelocation': sitelocation,
             'data': scraped_data,
             'form': scraped_form},
            context_instance=RequestContext(request))


@request_passes_test(_check_submit_permissions)
@get_sitelocation
@submit_lock
@csrf_protect
def embedrequest_submit_video(request, sitelocation=None):
    if not (request.REQUEST.get('url') and \
                url_re.match(request.REQUEST['url'])):
        return HttpResponseRedirect(reverse('localtv_submit_video'))

    url = request.REQUEST['url']
    existing =  models.Video.objects.filter(site=sitelocation.site,
                                            website_url=url)
    existing.filter(status=models.VIDEO_STATUS_REJECTED).delete()
    if existing.count():
        return HttpResponseRedirect(reverse('localtv_submit_thanks',
                                                args=[existing[0].id]))

    scraped_data = util.get_scraped_data(request.REQUEST['url']) or {}
    initial = {
        'url': url,
        'name': scraped_data.get('title', ''),
        'description': scraped_data.get('description', ''),
        'thumbnail': scraped_data.get('thumbnail_url', '')
        }
    if request.method == "GET":
        embed_form = forms.EmbedSubmitVideoForm(initial=initial)

        return render_to_response(
            'localtv/submit_video/embed.html',
            {'sitelocation': sitelocation,
             'form': embed_form},
            context_instance=RequestContext(request))

    embed_form = forms.EmbedSubmitVideoForm(request.POST, request.FILES,
                                            sitelocation=sitelocation,
                                            user=request.user)

    return _submit_finish(embed_form,
                          'localtv/submit_video/embed.html',
                          {'sitelocation': sitelocation,
                           'form': embed_form},
                          context_instance=RequestContext(request))


@request_passes_test(_check_submit_permissions)
@get_sitelocation
@submit_lock
@csrf_protect
def directlink_submit_video(request, sitelocation=None):
    if not (request.REQUEST.get('url') and \
                url_re.match(request.REQUEST['url'])):
        return HttpResponseRedirect(reverse('localtv_submit_video'))

    url = request.REQUEST['url']
    existing =  models.Video.objects.filter(Q(website_url=url)|Q(file_url=url),
                                            site=sitelocation.site)
    existing.filter(status=models.VIDEO_STATUS_REJECTED).delete()
    if existing.count():
        return HttpResponseRedirect(reverse('localtv_submit_thanks',
                                                args=[existing[0].id]))
    initial = dict(request.GET.items())
    if request.method == "GET":
        direct_form = forms.DirectSubmitVideoForm(initial=initial)

        return render_to_response(
            'localtv/submit_video/direct.html',
            {'sitelocation': sitelocation,
             'form': direct_form},
            context_instance=RequestContext(request))

    direct_form = forms.DirectSubmitVideoForm(request.POST, request.FILES,
                                              sitelocation=sitelocation,
                                              user=request.user)

    return _submit_finish(direct_form,
                          'localtv/submit_video/direct.html',
                          {'sitelocation': sitelocation,
                           'form': direct_form},
                          context_instance=RequestContext(request))


@get_sitelocation
def submit_thanks(request, video_id=None, sitelocation=None):
    if sitelocation.user_is_admin(request.user) and video_id:
        context = {
            'video': models.Video.objects.get(pk=video_id)
            }
    else:
        context = {}
    return render_to_response(
        'localtv/submit_video/thanks.html', context,
        context_instance=RequestContext(request))
