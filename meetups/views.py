from functools import wraps

import sendgrid

from . import app, meetup, sendgrid_api
from flask import render_template, redirect, url_for, request, session, flash
from flask.ext.login import current_user, login_required, login_user, logout_user

from .forms import (VenueEditForm, VenueClaimForm, RequestForSpaceForm,
    UserProfileForm, RequestForSpaceInitial)
from .logic import sync_user, get_unclaimed_venues, get_users_venues, get_groups, get_events, get_venues, event_cmp
from .models import User, Group, Venue, Event, login_manager


def skip_if_logged_in(func):
    """Decorator for functions in the login flow that skips to
    the destination if the user is already logged in.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        if current_user.is_authenticated():
            return redirect(url_for('user_profile'))
        return func(*args, **kwargs)
    return wrapper


@app.route('/clear/')
def clear():
    session.clear()
    return redirect('/')


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/have/')
def have():
    venues = get_unclaimed_venues()
    return render_template('have.html', venues=venues)


@app.route('/login/')
@app.route('/login/<string:service>/', methods=('GET', 'POST'))
@skip_if_logged_in
def login(service=''):
    if service:
        return meetup.authorize(callback=url_for('login_meetup_return'))
    else:
        return render_template('redirect_to_meetup.html')


@app.route('/login/meetup/return/', methods=('GET',))
@meetup.authorized_handler
@skip_if_logged_in
def login_meetup_return(oauth_response):
    session['meetup_token'] = (
        oauth_response['oauth_token'],
        oauth_response['oauth_token_secret']
    )
    session['member_id'] = oauth_response['member_id']
    return render_template('login.html')


@app.route('/login/sync/', methods=('GET',))
@skip_if_logged_in
def login_sync():
    user = sync_user(session['member_id'])
    login_user(user)
    redirect_to = session.pop('login_redirect', url_for('user_profile'))
    return redirect(redirect_to)


@app.route('/logout/')
def logout():
    session.pop('meetup_token', None)
    session.pop('meetup_member_id', None)
    session.pop('login_redirect', None)
    logout_user()
    return redirect(url_for('.index'))


@app.route('/need/')
@login_required
def need():
    user = User(_id=int(session['member_id'])).load()
    groups = get_groups({'_id': {'$in': user.organizer_of}})
    return render_template('need.html',
        user=user,
        groups=groups,
    )


@app.route('/need/group/<int:group_id>/')
@login_required
def need_event(group_id):
    user = User(_id=int(session['member_id'])).load()
    group = Group(_id=group_id).load()
    events = list(get_events({'group_id': group._id}))
    events.sort(event_cmp)
    return render_template('need.html',
        user=user,
        group=group,
        events=events,
    )


@app.route('/need/group/<int:group_id>/event/<event_id>/')
@login_required
def need_venue(group_id, event_id):
    user = User(_id=int(session['member_id'])).load()
    group = Group(_id=group_id).load()
    event = Event(_id=event_id).load()
    all_venues = get_venues({
        'loc': {'$near': user.loc},
        'claimed': True,
        'deleted': False,
    })
    return render_template('need.html',
        user=user,
        group=group,
        event=event,
        all_venues=all_venues,
    )


@app.route('/need/group/<int:group_id>/event/<event_id>/request/', methods=('POST',))
@login_required
def need_request(group_id, event_id, form=None):
    venue_ids = request.form.getlist('venue_id', type=int)
    if not venue_ids:
        flash(u'You need to pick at least one venue!', 'warning')
        return redirect(url_for('need_venue', group_id=group_id, event_id=event_id))

    user = User(_id=int(session['member_id'])).load()
    group = Group(_id=group_id).load()
    event = Event(_id=event_id).load()
    all_venues = get_venues({
        'loc': {'$near': user.loc},
        'claimed': True,
        'deleted': False,
    })
    picked_venues = []
    venue_ids = set(venue_ids)
    for venue in all_venues:
        if venue._id in venue_ids:
            picked_venues.append(venue)

    initial = RequestForSpaceInitial(user, event, group)
    request_form = form or RequestForSpaceForm(obj=initial)

    return render_template('need.html',
        user=user,
        group=group,
        event=event,
        picked_venues=picked_venues,
        request_form=request_form,
        event_size_known=hasattr(event, 'rsvp_limit'),
        event_time_known=hasattr(event, 'time'),
    )


@app.route('/need/group/<int:group_id>/event/<event_id>/request/submit/', methods=('POST',))
@login_required
def need_request_submit(group_id, event_id):
    user = User(_id=int(session['member_id'])).load()
    group = Group(_id=group_id).load()
    event = Event(_id=event_id).load()

    initial = RequestForSpaceInitial(user, event, group)

    form = RequestForSpaceForm(request.form, obj=initial)
    if not form.validate():
        flash(u'There were errors with the form', 'error')
        return need_request(group_id, event_id, form=form)

    venues = get_venues({
        '_id': {'$in': map(int, request.form.getlist('venue_id'))}})

    def evaluate_body(venue):
        body = form.body.data
        body = body.replace('{{host}}', venue.contact['name'])
        body = body.replace('{{venue_name}}', venue.name)
        return body

    for count, venue in enumerate(venues):
        recipient = venue.contact['email']
        body = evaluate_body(venue)
        message = sendgrid.Message(
            addr_from=form.email.data,
            subject="WhereToMeetup Request for Use of Your Space",
            text=body)
        message.add_to(recipient)
        sendgrid_api.smtp.send(message)

    if count > 1:
        flash(u'The hosts have been notified of your request', 'info')
    else:
        flash(u'The host has been notified of your request', 'info')

    return redirect(url_for('index'))


@app.route('/account/', methods=('GET', 'POST'))
@login_required
def user_profile():
    user = User(_id=int(session['member_id'])).load()

    form = UserProfileForm(request.form, obj=user)
    if request.method == 'POST' and form.validate():
        user.update_profile(email=form.email.data, phone=form.phone.data)
        flash('Your profile has been updated', 'success')
        return redirect(url_for('user_profile'))

    return render_template('account/profile.html', user=user, form=form)


@app.route('/space/<int:_id>/claim/', methods=('GET', 'POST'))
@login_required
def venue_claim(_id):
    venue = Venue(_id=_id).load()

    user = User(_id=int(session['member_id'])).load()

    # If the user has not email or phone number and the venue does, place
    # them on the user for the purpose for prepopulating the form.
    if not getattr(user, 'email', None) and getattr(venue, 'email', None):
        user.email = venue.email
    if not getattr(user, 'phone', None) and getattr(venue, 'phone', None):
        user.phone = venue.phone

    # There are different forms for editing and claiming a venue. Use the
    # right one.
    if venue.claimed:
        form_class = VenueEditForm
    else:
        form_class = VenueClaimForm

    # Check for current contact information linked to the venue. For any fields
    # that don't have a value, use the values associated with the user doing
    # the claiming.
    if hasattr(venue, 'contact'):
        venue.contact_name = venue.contact.get('name')
        venue.contact_email = venue.contact.get('email')
        venue.contact_phone = venue.contact.get('phone')
    else:
        venue.contact_name = None
        venue.contact_email = None
        venue.contact_phone = None
    if not venue.contact_name:
        venue.contact_name = user.name
    if not venue.contact_email:
        venue.contact_email = user.email
    if not venue.contact_phone:
        venue.contact_phone = user.phone

    form = form_class(request.form, obj=venue)
    if request.method == 'POST' and form.validate():
        venue.claim(name=form.contact_name.data, email=form.contact_email.data,
            phone=form.contact_phone.data, user_id=user._id,
            capacity=form.capacity.data)

        flash('Thank you for %s %s' % (
            'updating' if venue.claimed else 'claiming', venue.name), 'success')

        return redirect(url_for('venues_for_user'))

    return render_template('venue/claim.html', venue=venue, form=form)


@app.route('/account/spaces/')
@login_required
def venues_for_user():
    user = User(_id=int(session['member_id'])).load()

    venues = get_users_venues(user_id=user._id)
    return render_template('account/venues.html', venues=venues)


@meetup.tokengetter
def get_meetup_token():
    return session.get('meetup_token')


@login_manager.unauthorized_handler
def login_prompt():
    session['login_redirect'] = request.path
    return redirect(url_for('login'))
