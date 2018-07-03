# -*- coding: utf-8 -*-
# Generated by Django 1.11.13 on 2018-07-03 08:58
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rules', '0063_ruleprocessingfilter_ruleprocessingfilterdef'),
    ]

    operations = [
        migrations.AddField(
            model_name='ruleprocessingfilter',
            name='rulesets',
            field=models.ManyToManyField(related_name='processing_filters', to='rules.Ruleset'),
        ),
    ]
