# Generated for Payment.credit_applied

from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0006_alter_payment_method'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='credit_applied',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0.00'),
                max_digits=12,
            ),
        ),
    ]
