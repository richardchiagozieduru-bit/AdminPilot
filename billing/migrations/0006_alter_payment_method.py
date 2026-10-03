# Generated for PaymentMethod.CREDIT

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0005_paymentitemallocation_student_fee_item_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='payment',
            name='method',
            field=models.CharField(
                choices=[
                    ('cash', 'Cash'),
                    ('transfer', 'Transfer'),
                    ('card', 'Card'),
                    ('pos', 'POS'),
                    ('cheque', 'Cheque'),
                    ('credit', 'Student Credit'),
                    ('other', 'Other'),
                ],
                max_length=10,
            ),
        ),
    ]
